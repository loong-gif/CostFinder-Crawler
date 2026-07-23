"""Irvine Botox price discovery: merge Search hits, gate, Scrape, ingest raw tables.

Purpose: Process `.firecrawl/irvine-botox/search-*.json` from Firecrawl CLI runs,
dedupe, price-gate, scrape candidates, write `firecrawl_search_raw` / `firecrawl_scrape_raw`.

Inputs: `.firecrawl/irvine-botox/search-*.json` (from prior CLI search runs)
Outputs: merged-hits.json, scrape-candidates.json, scrape-*.json, ingestion-audit.json
DB writes: only with `--apply` (requires ALLOW_SERVICE_ROLE_WRITES=true)
Default: `--dry-run` (merge/gate/scrape local only unless `--apply`)

Status: in progress — run after search CLI batch completes.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import subprocess
import sys
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from crawler.staging_recrawl import load_supabase_client
from utils.firecrawl_scrape_raw_db import (
    canonical_scrape_url,
    save_scrape_response,
    scrape_request_fingerprint,
)
from utils.firecrawl_search_raw_db import save_search_queries, search_web_row, web_rows_from_search_file
from utils.scrape_markdown import prepare_scrape_markdown
from utils.search_scrape_gate import search_hit_has_price

OUT_DIR = PROJECT_ROOT / ".firecrawl" / "irvine-botox"
AUDIT_PATH = OUT_DIR / "ingestion-audit.json"

OPEN_QUERIES: dict[str, str] = {
    "search-primary.json": "irvine botox special price",
    "search-unit-price.json": "irvine botox price per unit",
    "search-medspa-offer.json": "irvine med spa botox special offer",
}
OPEN_WEBSITE = "irvine-ca-botox"
OPEN_DOMAIN = "irvine-ca"
SCRAPE_CONCURRENCY = 2
MIN_SEARCH_MD_LEN = 800  # ponytail: skip rescrape when search markdown already rich


@dataclass
class Hit:
    url: str
    title: str = ""
    description: str = ""
    markdown: str = ""
    source_query: str = ""
    source_file: str = ""


@dataclass
class SearchFileMeta:
    path: Path
    query: str
    website: str
    domain: str


def domain_slug(domain: str) -> str:
    return domain.replace(".", "-").replace("/", "-")


def query_for_file(path: Path) -> tuple[str, str, str]:
    """Return (query, website, domain) for a search JSON file."""
    name = path.name
    if name in OPEN_QUERIES:
        return OPEN_QUERIES[name], OPEN_WEBSITE, OPEN_DOMAIN
    if name.startswith("search-site-") and name.endswith(".json"):
        slug = name.removeprefix("search-site-").removesuffix(".json")
        domain = f"{slug}.com"
        query = f'site:{domain} botox (special OR price OR "per unit")'
        return query, domain, domain
    stem = name.removeprefix("search-").removesuffix(".json")
    return stem.replace("-", " "), OPEN_WEBSITE, OPEN_DOMAIN


def load_hits_from_file(meta: SearchFileMeta) -> list[Hit]:
    payload = json.loads(meta.path.read_text(encoding="utf-8"))
    web = payload.get("data", {}).get("web", []) if isinstance(payload, dict) else []
    hits: list[Hit] = []
    for item in web:
        if not isinstance(item, dict):
            continue
        url = str(item.get("url") or "").strip()
        if not url:
            continue
        hits.append(
            Hit(
                url=url,
                title=str(item.get("title") or ""),
                description=str(item.get("description") or ""),
                markdown=str(item.get("markdown") or ""),
                source_query=meta.query,
                source_file=meta.path.name,
            )
        )
    return hits


def merge_hits(all_hits: list[Hit]) -> list[Hit]:
    by_url: dict[str, Hit] = {}
    for hit in all_hits:
        canon = canonical_scrape_url(hit.url) or hit.url
        existing = by_url.get(canon)
        if existing is None:
            by_url[canon] = Hit(
                url=hit.url,
                title=hit.title,
                description=hit.description,
                markdown=hit.markdown,
                source_query=hit.source_query,
                source_file=hit.source_file,
            )
            continue
        if len(hit.markdown) > len(existing.markdown):
            existing.markdown = hit.markdown
        if hit.title and not existing.title:
            existing.title = hit.title
        if hit.description and not existing.description:
            existing.description = hit.description
    return list(by_url.values())


def hit_has_price(hit: Hit) -> bool:
    return search_hit_has_price(
        title=hit.title,
        markdown=hit.markdown,
        description=hit.description,
    )


def search_scrape_sufficient(hit: Hit) -> bool:
    md = prepare_scrape_markdown(hit.markdown)
    return len(md) >= MIN_SEARCH_MD_LEN and hit_has_price(Hit(url=hit.url, markdown=md))


def scrape_prefix(url: str) -> str:
    return hashlib.sha256(url.encode()).hexdigest()[:12]


def run_cli_scrape(url: str, out_path: Path) -> dict[str, Any]:
    cmd = [
        "firecrawl",
        "scrape",
        url,
        "--only-main-content",
        "--format",
        "markdown,links",
        "-o",
        str(out_path),
    ]
    proc = subprocess.run(cmd, capture_output=True, text=True, timeout=180)
    if proc.returncode != 0:
        raise RuntimeError(proc.stderr.strip() or proc.stdout.strip() or f"exit {proc.returncode}")
    if not out_path.exists():
        raise RuntimeError("scrape output missing")
    payload = json.loads(out_path.read_text(encoding="utf-8"))
    if isinstance(payload, dict) and payload.get("success") is False:
        raise RuntimeError(str(payload.get("error") or "scrape failed"))
    return payload


def extract_markdown(payload: dict[str, Any]) -> str:
    data = payload.get("data") if isinstance(payload.get("data"), dict) else payload
    if not isinstance(data, dict):
        return ""
    return prepare_scrape_markdown(str(data.get("markdown") or ""))


def pick_search_raw_id_for_scrape(source_url: str, search_raw_ids: dict[str, int]) -> int:
    url = str(source_url or "").lower()
    if any(token in url for token in ("botox", "dysport", "daxxify", "tox", "neurotoxin")):
        for query, row_id in search_raw_ids.items():
            q = query.lower()
            if any(token in q for token in ("botox", "dysport", "daxxify", "injectable", "tox")):
                return row_id
    if any(token in url for token in ("special", "promo", "offer", "deal")):
        for query, row_id in search_raw_ids.items():
            if any(token in query.lower() for token in ("special", "promo", "offer", "deal")):
                return row_id
    return next(iter(search_raw_ids.values()))


def discover_search_files() -> list[SearchFileMeta]:
    metas: list[SearchFileMeta] = []
    for path in sorted(OUT_DIR.glob("search-*.json")):
        query, website, domain = query_for_file(path)
        metas.append(SearchFileMeta(path=path, query=query, website=website, domain=domain))
    return metas


def cmd_merge_gate(*, max_scrape: int | None) -> dict[str, Any]:
    metas = discover_search_files()
    if not metas:
        raise RuntimeError(f"No search-*.json in {OUT_DIR}")

    all_hits: list[Hit] = []
    for meta in metas:
        all_hits.extend(load_hits_from_file(meta))

    merged = merge_hits(all_hits)
    priced = [h for h in merged if hit_has_price(h)]
    candidates = priced[:max_scrape] if max_scrape else priced

    OUT_DIR.mkdir(parents=True, exist_ok=True)
    merged_doc = {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "search_files": len(metas),
        "unique_urls": len(merged),
        "price_gated": len(priced),
        "hits": [
            {
                "url": h.url,
                "title": h.title,
                "description": h.description[:240],
                "markdown_len": len(h.markdown),
                "has_price": hit_has_price(h),
                "search_scrape_sufficient": search_scrape_sufficient(h),
                "source_query": h.source_query,
                "source_file": h.source_file,
            }
            for h in merged
        ],
    }
    (OUT_DIR / "merged-hits.json").write_text(
        json.dumps(merged_doc, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    scrape_doc = {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "candidate_count": len(candidates),
        "candidates": [
            {
                "url": h.url,
                "title": h.title,
                "search_scrape_sufficient": search_scrape_sufficient(h),
            }
            for h in candidates
        ],
    }
    (OUT_DIR / "scrape-candidates.json").write_text(
        json.dumps(scrape_doc, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    return {
        "search_files": len(metas),
        "unique_urls": len(merged),
        "price_gated": len(priced),
        "scrape_candidates": len(candidates),
    }


def cmd_scrape(*, max_scrape: int | None, force: bool) -> dict[str, Any]:
    path = OUT_DIR / "scrape-candidates.json"
    if not path.exists():
        raise RuntimeError("Run merge-gate first (scrape-candidates.json missing)")
    doc = json.loads(path.read_text(encoding="utf-8"))
    candidates = doc.get("candidates") or []

    successes: list[dict[str, Any]] = []
    failures: list[dict[str, Any]] = []
    skipped = 0

    to_scrape: list[dict[str, Any]] = []
    for item in candidates:
        url = str(item.get("url") or "").strip()
        if not url:
            continue
        out_path = OUT_DIR / f"scrape-{scrape_prefix(url)}.json"
        if item.get("search_scrape_sufficient") and out_path.exists() and not force:
            skipped += 1
            continue
        if item.get("search_scrape_sufficient") and not force:
            # Use search markdown as scrape output to save credits
            merged_path = OUT_DIR / "merged-hits.json"
            if merged_path.exists():
                merged = json.loads(merged_path.read_text(encoding="utf-8"))
                for hit in merged.get("hits") or []:
                    if canonical_scrape_url(str(hit.get("url") or "")) == canonical_scrape_url(url):
                        md_len = int(hit.get("markdown_len") or 0)
                        if md_len >= MIN_SEARCH_MD_LEN:
                            skipped += 1
                            break
                else:
                    to_scrape.append(item)
                continue
        to_scrape.append(item)

    if max_scrape is not None:
        to_scrape = to_scrape[:max_scrape]

    def _one(item: dict[str, Any]) -> tuple[str, dict[str, Any] | None, str | None]:
        url = str(item["url"])
        out_path = OUT_DIR / f"scrape-{scrape_prefix(url)}.json"
        try:
            payload = run_cli_scrape(url, out_path)
            md = extract_markdown(payload)
            if not md or not search_hit_has_price(markdown=md):
                return url, None, "no price in scraped markdown"
            return url, {"path": str(out_path), "markdown_len": len(md), "payload": payload}, None
        except Exception as exc:
            return url, None, str(exc)

    with ThreadPoolExecutor(max_workers=SCRAPE_CONCURRENCY) as pool:
        futures = {pool.submit(_one, item): item for item in to_scrape}
        for fut in as_completed(futures):
            url, result, err = fut.result()
            if err:
                failures.append({"url": url, "error": err})
            else:
                successes.append({"url": url, **(result or {})})

    fail_doc = {"failures": failures, "generated_at": datetime.now(timezone.utc).isoformat()}
    (OUT_DIR / "scrape-failures.json").write_text(
        json.dumps(fail_doc, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    return {
        "scrape_attempted": len(to_scrape),
        "scrape_success": len(successes),
        "scrape_failed": len(failures),
        "scrape_skipped_sufficient": skipped,
    }


def cmd_ingest(*, apply: bool) -> dict[str, Any]:
    metas = discover_search_files()
    grouped: dict[tuple[str, str], list[tuple[str, list[dict[str, Any]]]]] = {}
    for meta in metas:
        rows = web_rows_from_search_file(meta.path)
        if not rows:
            continue
        key = (meta.website, meta.domain)
        grouped.setdefault(key, []).append((meta.query, rows))

    search_raw_ids: dict[str, int] = {}
    search_rows_written = 0

    if apply:
        os.environ.setdefault("ALLOW_SERVICE_ROLE_WRITES", "true")
        client = load_supabase_client()
        for (website, domain), entries in grouped.items():
            ids = save_search_queries(client, website=website, domain=domain, entries=entries)
            search_raw_ids.update(ids)
            search_rows_written += len(ids)
    else:
        for meta in metas:
            search_raw_ids[meta.query] = -1
            search_rows_written += 1

    scrape_written = 0
    scrape_failures = 0
    merged_path = OUT_DIR / "merged-hits.json"
    merged_by_url: dict[str, dict[str, Any]] = {}
    if merged_path.exists():
        for hit in json.loads(merged_path.read_text()).get("hits") or []:
            u = canonical_scrape_url(str(hit.get("url") or ""))
            if u:
                merged_by_url[u] = hit

    candidates_path = OUT_DIR / "scrape-candidates.json"
    candidate_urls = [
        str(c.get("url") or "")
        for c in json.loads(candidates_path.read_text()).get("candidates") or []
        if c.get("url")
    ]

    if apply:
        client = load_supabase_client()
        for url in candidate_urls:
            canon = canonical_scrape_url(url)
            out_path = OUT_DIR / f"scrape-{scrape_prefix(url)}.json"
            fp = scrape_request_fingerprint(url, only_main_content=True)
            search_raw_id = pick_search_raw_id_for_scrape(url, search_raw_ids)

            body: dict[str, Any] | None = None
            if out_path.exists():
                body = json.loads(out_path.read_text(encoding="utf-8"))
            elif canon in merged_by_url:
                # Fall back to search markdown embedded in merged hits
                hit = merged_by_url[canon]
                for meta in metas:
                    file_hits = load_hits_from_file(meta)
                    for h in file_hits:
                        if canonical_scrape_url(h.url) == canon and h.markdown:
                            md = prepare_scrape_markdown(h.markdown)
                            body = {"data": {"markdown": md, "metadata": {"source": "search_scrape"}}}
                            break
                    if body:
                        break

            if not body:
                scrape_failures += 1
                continue

            md = extract_markdown(body) if body else ""
            if not md or not search_hit_has_price(markdown=md):
                save_scrape_response(
                    client,
                    fp,
                    url,
                    None,
                    search_raw_id=search_raw_id if search_raw_id > 0 else None,
                    success=False,
                    error_message="no price evidence after scrape",
                )
                scrape_failures += 1
                continue

            if isinstance(body.get("data"), dict):
                body = {**body, "data": {**body["data"], "markdown": md}}
            save_scrape_response(
                client,
                fp,
                url,
                body,
                search_raw_id=search_raw_id if search_raw_id > 0 else None,
                success=True,
            )
            scrape_written += 1

    audit = {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "apply": apply,
        "search_files": len(metas),
        "search_rows_written": search_rows_written,
        "search_raw_ids": search_raw_ids,
        "scrape_candidates": len(candidate_urls),
        "scrape_rows_written": scrape_written,
        "scrape_failures": scrape_failures,
    }
    AUDIT_PATH.write_text(json.dumps(audit, ensure_ascii=False, indent=2), encoding="utf-8")
    return audit


def parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(description="Irvine Botox search merge/gate/scrape/ingest")
    p.add_argument("--apply", action="store_true", help="Write Supabase raw tables")
    p.add_argument("--merge-only", action="store_true")
    p.add_argument("--scrape-only", action="store_true")
    p.add_argument("--ingest-only", action="store_true")
    p.add_argument("--force-scrape", action="store_true", help="Rescrape even when search md sufficient")
    p.add_argument("--max-scrape", type=int, default=None, help="Cap scrape attempts (credit guard)")
    return p.parse_args()


def main() -> None:
    args = parse_args()
    stats: dict[str, Any] = {}

    if args.ingest_only:
        stats["ingest"] = cmd_ingest(apply=args.apply)
    elif args.scrape_only:
        stats["scrape"] = cmd_scrape(max_scrape=args.max_scrape, force=args.force_scrape)
    elif args.merge_only:
        stats["merge"] = cmd_merge_gate(max_scrape=args.max_scrape)
    else:
        stats["merge"] = cmd_merge_gate(max_scrape=args.max_scrape)
        stats["scrape"] = cmd_scrape(max_scrape=args.max_scrape, force=args.force_scrape)
        stats["ingest"] = cmd_ingest(apply=args.apply)

    print(json.dumps(stats, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
