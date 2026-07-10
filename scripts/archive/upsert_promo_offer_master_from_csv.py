#!/usr/bin/env python3
"""
Import promo offer rows from CSV into promo_offer_master.

Matching rule:
- exact same normalized source_url
- highly similar offer_raw_text

Rows that match an existing record are updated in place.
Rows that do not match are inserted as new records.
"""
from __future__ import annotations

import argparse
import csv
import json
import os
import re
import sys
from collections import defaultdict
from datetime import datetime, timezone
from difflib import SequenceMatcher
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Sequence, Tuple
from urllib.parse import parse_qsl, urlsplit, urlunsplit

import requests
from dotenv import load_dotenv

PROJECT_ROOT = Path(__file__).resolve().parent.parent
OUTPUT_DIR = PROJECT_ROOT / "output" / "results"
TABLE_NAME = "promo_offer_master"
PAGE_SIZE = 1000
INSERT_BATCH_SIZE = 50
DEFAULT_MATCH_THRESHOLD = 0.9

TRACKING_QUERY_KEYS = {
    "utm_source",
    "utm_medium",
    "utm_campaign",
    "utm_term",
    "utm_content",
    "gclid",
    "fbclid",
    "srsltid",
}

TEXT_COLUMNS = {
    "channel",
    "source_url",
    "source_name",
    "template_type",
    "service_category",
    "service_name",
    "offer_raw_text",
    "start_date",
    "end_date",
    "eligibility",
    "service_area",
    "unit_type",
    "membership_name",
    "billing_period",
    "minimum_term",
    "cancellation_policy",
}

NUMERIC_COLUMNS = {
    "original_price",
    "discount_price",
    "discount_amount",
    "discount_percent",
    "membership_price",
}

TEXT_BOOL_COLUMNS = {
    "is_package",
    "is_membership_required",
}


class SupabaseRestClient:
    def __init__(self, base_url: str, service_role_key: str):
        self.raw_base_url = base_url.rstrip("/")
        self.base_url = self.raw_base_url + "/rest/v1"
        self.session = requests.Session()
        self.session.trust_env = False
        self.session.headers.update(
            {
                "apikey": service_role_key,
                "Authorization": f"Bearer {service_role_key}",
                "Accept": "application/json",
                "Content-Type": "application/json",
            }
        )

    def fetch_rows(
        self,
        table: str,
        select: str,
        *,
        limit: int,
        offset: int,
        order: str,
    ) -> List[Dict[str, Any]]:
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
            params={"id": f"eq.{row_id}"},
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


def load_client() -> SupabaseRestClient:
    load_dotenv(PROJECT_ROOT / ".env")
    base_url = os.getenv("SUPABASE_URL")
    service_role_key = os.getenv("SUPABASE_SERVICE_ROLE_KEY")
    if not base_url or not service_role_key:
        raise RuntimeError("缺少 SUPABASE_URL 或 SUPABASE_SERVICE_ROLE_KEY")
    return SupabaseRestClient(base_url, service_role_key)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="把 CSV 导入 promo_offer_master，并按 source_url + offer_raw_text 相似度更新重复行")
    parser.add_argument("--csv", required=True, help="输入 CSV 文件路径")
    parser.add_argument("--match-threshold", type=float, default=DEFAULT_MATCH_THRESHOLD, help="重复判定阈值，默认 0.9")
    parser.add_argument("--dry-run", action="store_true", help="只生成报告，不写入 Supabase")
    return parser.parse_args()


def normalize_url(value: Any) -> str:
    raw = str(value or "").strip()
    if not raw:
        return ""
    if not raw.startswith(("http://", "https://")):
        raw = f"https://{raw}"
    parsed = urlsplit(raw)
    netloc = parsed.netloc.lower()
    if netloc.startswith("www."):
        netloc = netloc[4:]
    query = [
        (key, value)
        for key, value in parse_qsl(parsed.query, keep_blank_values=True)
        if key.lower() not in TRACKING_QUERY_KEYS
    ]
    normalized = urlunsplit(
        (
            parsed.scheme.lower() or "https",
            netloc,
            parsed.path.rstrip("/"),
            "&".join(f"{key}={value}" if value else key for key, value in query),
            "",
        )
    )
    return normalized.rstrip("/")


def normalize_text(value: Any) -> str:
    text = str(value or "")
    text = text.replace("\r", " ").replace("\n", " ")
    text = text.replace("’", "'").replace("“", '"').replace("”", '"')
    text = text.replace("\xa0", " ")
    text = re.sub(r"https?://\S+", " ", text, flags=re.IGNORECASE)
    text = re.sub(r"[^a-zA-Z0-9\u4e00-\u9fff%$]+", " ", text)
    text = re.sub(r"\s+", " ", text).strip().casefold()
    return text


def text_similarity(left: Any, right: Any) -> float:
    a = normalize_text(left)
    b = normalize_text(right)
    if not a or not b:
        return 0.0
    if a == b:
        return 1.0
    seq_ratio = SequenceMatcher(None, a, b, autojunk=False).ratio()
    tokens_a = set(a.split())
    tokens_b = set(b.split())
    token_union = tokens_a | tokens_b
    token_ratio = len(tokens_a & tokens_b) / len(token_union) if token_union else 0.0
    return max(seq_ratio, token_ratio)


def normalize_bool_text(value: Any) -> str:
    raw = str(value or "").strip().upper()
    if raw in {"TRUE", "T", "1", "YES", "Y"}:
        return "TRUE"
    if raw in {"FALSE", "F", "0", "NO", "N"}:
        return "FALSE"
    return ""


def normalize_numeric(value: Any) -> Optional[float]:
    raw = str(value or "").strip()
    if not raw:
        return None
    raw = raw.replace(",", "")
    try:
        return float(raw)
    except ValueError:
        return None


def parse_offer_content(raw: Any) -> Optional[Any]:
    text = str(raw or "").strip()
    if not text:
        return None
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        return None


def infer_template_type(row: Dict[str, Any], offer_content: Optional[Any]) -> str:
    source_name = str(row.get("source_name") or "").strip()
    service_name = str(row.get("service_name") or "").strip()
    membership_name = str(row.get("membership_name") or "").strip()
    is_membership_required = normalize_bool_text(row.get("is_membership_required")) == "TRUE"
    is_package = normalize_bool_text(row.get("is_package")) == "TRUE"

    if is_membership_required or membership_name:
        return "MEMBERSHIP"
    if is_package:
        return "BUNDLE"
    if isinstance(offer_content, dict) and len(offer_content) > 1:
        return "BUNDLE"
    if normalize_numeric(row.get("discount_percent")) is not None or normalize_numeric(row.get("discount_amount")) is not None:
        return "DISCOUNT"
    if normalize_numeric(row.get("discount_price")) is not None and normalize_numeric(row.get("original_price")) is not None:
        return "DISCOUNT"
    if "complimentary" in normalize_text(source_name) or "complimentary" in normalize_text(service_name):
        return "COMPLIMENTARY"
    return "FIXED_PRICE"


def read_csv_rows(csv_path: Path) -> List[Dict[str, Any]]:
    with csv_path.open("r", encoding="utf-8-sig", newline="") as handle:
        reader = csv.DictReader(handle)
        rows: List[Dict[str, Any]] = []
        for index, raw in enumerate(reader, start=2):
            source_url = str(raw.get("source_url") or "").strip()
            offer_raw_text = str(raw.get("offer_raw_text") or "").strip()
            if not source_url or not offer_raw_text:
                continue
            row = {key: str(value or "").strip() for key, value in raw.items()}
            row["_csv_row_number"] = index
            rows.append(row)

    deduped: Dict[Tuple[str, str], Dict[str, Any]] = {}
    for row in rows:
        key = (normalize_url(row.get("source_url")), normalize_text(row.get("offer_raw_text")))
        if not key[0] or not key[1]:
            continue
        deduped.setdefault(key, row)
    return list(deduped.values())


def fetch_existing_rows(client: SupabaseRestClient) -> List[Dict[str, Any]]:
    rows: List[Dict[str, Any]] = []
    offset = 0
    while True:
        batch = client.fetch_rows(TABLE_NAME, "*", limit=PAGE_SIZE, offset=offset, order="id.asc")
        if not batch:
            break
        rows.extend(batch)
        if len(batch) < PAGE_SIZE:
            break
        offset += PAGE_SIZE
    return rows


def build_update_payload(row: Dict[str, Any], existing_row: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    payload: Dict[str, Any] = {}
    offer_content = parse_offer_content(row.get("offer_content"))

    for key in TEXT_COLUMNS:
        if key in {"channel"}:
            continue
        raw = str(row.get(key) or "").strip()
        if raw:
            payload[key] = raw

    for key in NUMERIC_COLUMNS:
        value = normalize_numeric(row.get(key))
        if value is not None:
            payload[key] = value

    for key in TEXT_BOOL_COLUMNS:
        value = normalize_bool_text(row.get(key))
        if value:
            payload[key] = value

    if offer_content is not None:
        payload["offer_content"] = offer_content

    if existing_row is not None:
        # Only include changed fields when we are updating an existing row.
        filtered: Dict[str, Any] = {}
        for key, value in payload.items():
            current = existing_row.get(key)
            if isinstance(value, (dict, list)):
                try:
                    current_json = json.dumps(current, ensure_ascii=False, sort_keys=True)
                    next_json = json.dumps(value, ensure_ascii=False, sort_keys=True)
                except TypeError:
                    current_json = str(current)
                    next_json = str(value)
                if current_json != next_json:
                    filtered[key] = value
                continue
            if isinstance(value, float):
                try:
                    current_value = float(current) if current is not None else None
                except (TypeError, ValueError):
                    current_value = None
                if current_value != value:
                    filtered[key] = value
                continue
            if str(current if current is not None else "").strip() != str(value).strip():
                filtered[key] = value
        payload = filtered

    return payload


def build_insert_payload(row: Dict[str, Any]) -> Dict[str, Any]:
    offer_content = parse_offer_content(row.get("offer_content"))
    payload: Dict[str, Any] = {
        "channel": "Website",
        "source_url": str(row.get("source_url") or "").strip(),
        "source_name": str(row.get("source_name") or "").strip(),
        "service_category": str(row.get("service_category") or "").strip() or None,
        "service_name": str(row.get("service_name") or "").strip(),
        "offer_raw_text": str(row.get("offer_raw_text") or "").strip(),
        "eligibility": str(row.get("eligibility") or "").strip() or None,
        "service_area": str(row.get("service_area") or "").strip() or None,
        "unit_type": str(row.get("unit_type") or "").strip() or None,
        "membership_name": str(row.get("membership_name") or "").strip() or None,
        "billing_period": str(row.get("billing_period") or "").strip() or None,
        "minimum_term": str(row.get("minimum_term") or "").strip() or None,
        "cancellation_policy": str(row.get("cancellation_policy") or "").strip() or None,
        "template_type": infer_template_type(row, offer_content),
        "is_package": normalize_bool_text(row.get("is_package")) or None,
        "is_membership_required": normalize_bool_text(row.get("is_membership_required")) or None,
        "start_date": str(row.get("start_date") or "").strip() or None,
        "end_date": str(row.get("end_date") or "").strip() or None,
        "original_price": normalize_numeric(row.get("original_price")),
        "discount_price": normalize_numeric(row.get("discount_price")),
        "discount_amount": normalize_numeric(row.get("discount_amount")),
        "discount_percent": normalize_numeric(row.get("discount_percent")),
        "membership_price": normalize_numeric(row.get("membership_price")),
        "delivered_unit": str(row.get("delivered_unit") or "").strip() or None,
        "min_unit": str(row.get("min_unit") or "").strip() or None,
        "moderation_status": "approved",
    }
    if offer_content is not None:
        payload["offer_content"] = offer_content

    return {key: value for key, value in payload.items() if value is not None and value != ""}


def chunked(items: Sequence[Dict[str, Any]], size: int) -> Iterable[List[Dict[str, Any]]]:
    for start in range(0, len(items), max(1, size)):
        yield list(items[start : start + max(1, size)])


def row_fingerprint(row: Dict[str, Any]) -> Tuple[str, str]:
    return (normalize_url(row.get("source_url")), normalize_text(row.get("offer_raw_text")))


def normalized_service_name(value: Any) -> str:
    return normalize_text(value)


def build_report_path() -> Path:
    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    return OUTPUT_DIR / f"promo_offer_master_csv_upsert_{timestamp}.json"


def main() -> None:
    args = parse_args()
    csv_path = Path(args.csv).expanduser().resolve()
    if not csv_path.exists():
        raise FileNotFoundError(f"找不到 CSV 文件: {csv_path}")

    input_rows = read_csv_rows(csv_path)
    client = load_client()
    existing_rows = fetch_existing_rows(client)

    existing_by_url: Dict[str, List[Dict[str, Any]]] = defaultdict(list)
    for row in existing_rows:
        normalized_url = normalize_url(row.get("source_url"))
        if normalized_url:
            existing_by_url[normalized_url].append(row)

    matched_existing_ids: set[int] = set()
    update_plan: List[Dict[str, Any]] = []
    insert_plan: List[Dict[str, Any]] = []
    skipped_plan: List[Dict[str, Any]] = []

    for row in input_rows:
        normalized_url, normalized_text = row_fingerprint(row)
        candidates = [item for item in existing_by_url.get(normalized_url, []) if int(item.get("id") or 0) not in matched_existing_ids]
        candidate_scores: List[Tuple[float, Dict[str, Any]]] = []
        for candidate in candidates:
            score = text_similarity(row.get("offer_raw_text"), candidate.get("offer_raw_text"))
            candidate_scores.append((score, candidate))
        candidate_scores.sort(key=lambda item: (-item[0], int(item[1].get("id") or 0)))

        best_candidate: Optional[Dict[str, Any]] = None
        best_score = 0.0
        second_score = 0.0
        if candidate_scores:
            best_score, best_candidate = candidate_scores[0]
            if len(candidate_scores) > 1:
                second_score = candidate_scores[1][0]

        candidate_service_name = normalized_service_name(best_candidate.get("service_name")) if best_candidate is not None else ""
        input_service_name = normalized_service_name(row.get("service_name"))
        confident_match = False
        if best_candidate is not None:
            exact_text_match = normalized_text == normalize_text(best_candidate.get("offer_raw_text"))
            exact_service_match = bool(input_service_name and candidate_service_name and input_service_name == candidate_service_name)
            strong_unique_match = best_score >= max(args.match_threshold, 0.98) and (best_score - second_score) >= 0.05
            confident_match = exact_text_match or (best_score >= args.match_threshold and exact_service_match) or strong_unique_match

        if best_candidate is not None and confident_match:
            payload = build_update_payload(row, existing_row=best_candidate)
            existing_id = int(best_candidate["id"])
            if payload:
                update_plan.append(
                    {
                        "id": existing_id,
                        "score": round(best_score, 4),
                        "source_url": row.get("source_url"),
                        "source_name": row.get("source_name"),
                        "service_name": row.get("service_name"),
                        "payload": payload,
                        "csv_row_number": row.get("_csv_row_number"),
                        "second_best_score": round(second_score, 4),
                        "match_reason": (
                            "exact_offer_raw_text"
                            if normalized_text == normalize_text(best_candidate.get("offer_raw_text"))
                            else "service_name_match"
                            if input_service_name and candidate_service_name and input_service_name == candidate_service_name
                            else "high_score_margin"
                        ),
                    }
                )
            else:
                skipped_plan.append(
                    {
                        "id": existing_id,
                        "score": round(best_score, 4),
                        "source_url": row.get("source_url"),
                        "source_name": row.get("source_name"),
                        "service_name": row.get("service_name"),
                        "reason": "already_identical",
                        "csv_row_number": row.get("_csv_row_number"),
                        "match_reason": (
                            "exact_offer_raw_text"
                            if normalized_text == normalize_text(best_candidate.get("offer_raw_text"))
                            else "service_name_match"
                            if input_service_name and candidate_service_name and input_service_name == candidate_service_name
                            else "high_score_margin"
                        ),
                    }
                )
            matched_existing_ids.add(existing_id)
            continue

        insert_plan.append(
            {
                "source_url": row.get("source_url"),
                "source_name": row.get("source_name"),
                "service_name": row.get("service_name"),
                "score": round(best_score, 4) if best_candidate is not None else None,
                "csv_row_number": row.get("_csv_row_number"),
                "payload": build_insert_payload(row),
            }
        )

    updated_rows = 0
    inserted_rows = 0
    update_errors: List[Dict[str, Any]] = []
    insert_errors: List[Dict[str, Any]] = []

    if not args.dry_run:
        for item in update_plan:
            try:
                payload = item["payload"]
                if payload:
                    client.update_row(TABLE_NAME, int(item["id"]), payload)
                    updated_rows += 1
                else:
                    skipped_plan.append(
                        {
                            "id": item["id"],
                            "score": item["score"],
                            "source_url": item["source_url"],
                            "source_name": item["source_name"],
                            "service_name": item["service_name"],
                            "reason": "no_changed_fields",
                            "csv_row_number": item["csv_row_number"],
                        }
                    )
            except Exception as exc:  # noqa: BLE001
                update_errors.append({**item, "error": str(exc)})

        insert_rows = [item["payload"] for item in insert_plan]
        for batch in chunked(insert_rows, INSERT_BATCH_SIZE):
            try:
                result = client.insert_rows(TABLE_NAME, batch)
                inserted_rows += len(result)
            except Exception as exc:  # noqa: BLE001
                insert_errors.append(
                    {
                        "batch_size": len(batch),
                        "source_urls": [str(item.get("source_url", "")) for item in batch[:10]],
                        "error": str(exc),
                    }
                )

    report = {
        "dry_run": bool(args.dry_run),
        "csv_path": str(csv_path),
        "table": TABLE_NAME,
        "source_rows": len(input_rows),
        "existing_rows_loaded": len(existing_rows),
        "matched_update_rows": len(update_plan),
        "matched_skip_rows": len(skipped_plan),
        "insert_rows": len(insert_plan),
        "updated_rows": updated_rows,
        "inserted_rows": inserted_rows,
        "update_error_count": len(update_errors),
        "insert_error_count": len(insert_errors),
        "match_threshold": args.match_threshold,
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "sample_updates": update_plan[:20],
        "sample_inserts": insert_plan[:20],
        "sample_skips": skipped_plan[:20],
        "update_errors": update_errors[:20],
        "insert_errors": insert_errors[:20],
    }

    report_path = build_report_path()
    report_path.parent.mkdir(parents=True, exist_ok=True)
    report_path.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    print(
        json.dumps(
            {
                "report_path": str(report_path),
                "dry_run": report["dry_run"],
                "source_rows": report["source_rows"],
                "matched_update_rows": report["matched_update_rows"],
                "matched_skip_rows": report["matched_skip_rows"],
                "insert_rows": report["insert_rows"],
                "updated_rows": report["updated_rows"],
                "inserted_rows": report["inserted_rows"],
                "update_error_count": report["update_error_count"],
                "insert_error_count": report["insert_error_count"],
            },
            ensure_ascii=False,
            indent=2,
        )
    )


if __name__ == "__main__":
    main()
