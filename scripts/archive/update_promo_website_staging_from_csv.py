#!/usr/bin/env python3
"""
Update promo_website_staging from a CSV file.

Expected CSV columns:
- subpage_url
- page_content
- domain
- name
"""
from __future__ import annotations

import argparse
import csv
import json
import os
import re
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, Iterable, List
from urllib.parse import urlsplit

import requests
from dotenv import load_dotenv

PROJECT_ROOT = Path(__file__).resolve().parent.parent
OUTPUT_DIR = PROJECT_ROOT / "output" / "results"
TABLE = "promo_website_staging"
PAGE_SIZE = 1000


class SupabaseRestClient:
    def __init__(self, base_url: str, service_role_key: str):
        self.raw_base_url = base_url.rstrip("/")
        self.service_role_key = service_role_key
        self.base_url = self.raw_base_url + "/rest/v1"
        self.session = requests.Session()
        self.session.headers.update(
            {
                "apikey": service_role_key,
                "Authorization": f"Bearer {service_role_key}",
                "Accept": "application/json",
                "Content-Type": "application/json",
            }
        )

    def fetch_rows(self, table: str, select: str, *, limit: int, offset: int, order: str) -> List[Dict[str, Any]]:
        response = self.session.get(
            f"{self.base_url}/{table}",
            params={"select": select, "limit": str(limit), "offset": str(offset), "order": order},
            timeout=60,
        )
        response.raise_for_status()
        return response.json()

    def update_row(self, table: str, row_id: int, payload: Dict[str, Any]) -> List[Dict[str, Any]]:
        response = self.session.patch(
            f"{self.base_url}/{table}",
            params={"promo_website_id": f"eq.{row_id}"},
            headers={"Prefer": "return=representation"},
            json=payload,
            timeout=60,
        )
        response.raise_for_status()
        return response.json()

    def insert_rows(self, table: str, rows: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
        response = self.session.post(
            f"{self.base_url}/{table}",
            headers={"Prefer": "return=representation"},
            json=rows,
            timeout=60,
        )
        response.raise_for_status()
        return response.json()


def normalize_domain(value: Any) -> str:
    text = str(value or "").strip().lower()
    text = re.sub(r"^https?://", "", text)
    text = text.split("/")[0].split("?")[0].split("#")[0]
    if text.startswith("www."):
        text = text[4:]
    return text.strip(".")


def load_client() -> SupabaseRestClient:
    load_dotenv(PROJECT_ROOT / ".env")
    base_url = os.getenv("SUPABASE_URL")
    service_role_key = os.getenv("SUPABASE_SERVICE_ROLE_KEY")
    if not base_url or not service_role_key:
        raise RuntimeError("Missing SUPABASE_URL or SUPABASE_SERVICE_ROLE_KEY")
    return SupabaseRestClient(base_url, service_role_key)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Update promo_website_staging from CSV")
    parser.add_argument("--csv", required=True, help="Input CSV path")
    parser.add_argument("--workers", type=int, default=8, help="并发更新线程数（默认: 8）")
    parser.add_argument(
        "--domain-from-subpage-url",
        action="store_true",
        help="Use subpage_url host as domain_name (override CSV domain column)",
    )
    parser.add_argument("--dry-run", action="store_true", help="Preview only, do not write Supabase")
    return parser.parse_args()


def read_csv_rows(csv_path: Path, *, domain_from_subpage_url: bool) -> List[Dict[str, Any]]:
    with csv_path.open("r", encoding="utf-8-sig", newline="") as handle:
        reader = csv.DictReader(handle)
        rows: List[Dict[str, Any]] = []
        for raw in reader:
            subpage_url = str(raw.get("subpage_url") or "").strip()
            page_content = str(raw.get("page_content") or "").strip()
            if not subpage_url or not page_content:
                continue
            csv_domain = normalize_domain(raw.get("domain"))
            url_domain = normalize_domain(urlsplit(subpage_url).netloc)
            domain_name = url_domain if domain_from_subpage_url else (csv_domain or url_domain)
            rows.append(
                {
                    "subpage_url": subpage_url,
                    "page_content": page_content,
                    "domain_name": domain_name,
                    "name": str(raw.get("name") or "").strip(),
                }
            )
    deduped: Dict[str, Dict[str, Any]] = {}
    for row in rows:
        deduped[row["subpage_url"]] = row
    return list(deduped.values())


def fetch_existing_map(client: SupabaseRestClient) -> Dict[str, Dict[str, Any]]:
    existing: Dict[str, Dict[str, Any]] = {}
    offset = 0
    while True:
        batch = client.fetch_rows(
            TABLE,
            "promo_website_id,subpage_url,domain_name,name",
            limit=PAGE_SIZE,
            offset=offset,
            order="promo_website_id.asc",
        )
        if not batch:
            break
        for row in batch:
            subpage_url = str(row.get("subpage_url") or "").strip()
            if subpage_url:
                existing[subpage_url] = row
        if len(batch) < PAGE_SIZE:
            break
        offset += PAGE_SIZE
    return existing


def chunked(items: Iterable[Dict[str, Any]], size: int) -> Iterable[List[Dict[str, Any]]]:
    buf: List[Dict[str, Any]] = []
    for item in items:
        buf.append(item)
        if len(buf) >= size:
            yield buf
            buf = []
    if buf:
        yield buf


def resolve_report_path() -> Path:
    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    return OUTPUT_DIR / f"promo_website_staging_csv_update_{timestamp}.json"


def main() -> None:
    args = parse_args()
    csv_path = Path(args.csv).expanduser().resolve()
    if not csv_path.exists():
        raise FileNotFoundError(f"CSV not found: {csv_path}")

    input_rows = read_csv_rows(csv_path, domain_from_subpage_url=args.domain_from_subpage_url)
    client = load_client()
    existing_map = fetch_existing_map(client)
    now_iso = datetime.now(timezone.utc).isoformat()

    to_update: List[Dict[str, Any]] = []
    to_insert: List[Dict[str, Any]] = []

    for row in input_rows:
        payload = {
            "crawl_timestamp": now_iso,
            "subpage_url": row["subpage_url"],
            "page_content": row["page_content"],
            "domain_name": row["domain_name"],
            "processed_status": False,
            "name": row["name"],
        }
        matched = existing_map.get(row["subpage_url"])
        if matched:
            to_update.append(
                {
                    "promo_website_id": int(matched["promo_website_id"]),
                    "subpage_url": row["subpage_url"],
                    "payload": payload,
                }
            )
        else:
            to_insert.append(payload)

    updated_rows = 0
    inserted_rows = 0
    update_errors: List[Dict[str, Any]] = []
    if not args.dry_run:
        if to_update:
            workers = max(1, int(args.workers))

            def _update_worker(item: Dict[str, Any]) -> Dict[str, Any]:
                worker_client = SupabaseRestClient(client.raw_base_url, client.service_role_key)
                try:
                    worker_client.update_row(TABLE, item["promo_website_id"], item["payload"])
                    return {"ok": True, "subpage_url": item["subpage_url"]}
                except Exception as exc:  # noqa: BLE001
                    return {
                        "ok": False,
                        "subpage_url": item["subpage_url"],
                        "promo_website_id": item["promo_website_id"],
                        "error": str(exc),
                    }

            with ThreadPoolExecutor(max_workers=workers) as pool:
                futures = [pool.submit(_update_worker, item) for item in to_update]
                for future in as_completed(futures):
                    result = future.result()
                    if result.get("ok"):
                        updated_rows += 1
                    else:
                        update_errors.append(result)

        for batch in chunked(to_insert, 100):
            result = client.insert_rows(TABLE, batch)
            inserted_rows += len(result)

    report = {
        "status": "dry_run" if args.dry_run else "completed",
        "table": TABLE,
        "csv_path": str(csv_path),
        "total_input_rows": len(input_rows),
        "to_update_rows": len(to_update),
        "to_insert_rows": len(to_insert),
        "updated_rows": updated_rows,
        "inserted_rows": inserted_rows,
        "update_error_count": len(update_errors),
        "update_errors_sample": update_errors[:20],
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "sample_update_urls": [item["subpage_url"] for item in to_update[:10]],
        "sample_insert_urls": [item["subpage_url"] for item in to_insert[:10]],
    }
    report_path = resolve_report_path()
    report_path.parent.mkdir(parents=True, exist_ok=True)
    report_path.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps({"report_path": str(report_path), **report}, ensure_ascii=False))


if __name__ == "__main__":
    main()
