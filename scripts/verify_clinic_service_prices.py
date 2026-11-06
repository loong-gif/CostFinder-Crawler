#!/usr/bin/env python3
"""Verify clinic_services regular_price against live Firecrawl scrape (+ cache fallback).

Reads exported services CSV, scrapes distinct source_urls once, extracts per-service
regular price + normalized evidence, writes audit CSV. Does NOT update Supabase.

Usage:
  python3 scripts/verify_clinic_service_prices.py \\
    --services-csv output/clinic_services_export.csv \\
    --cache-jsonl output/firecrawl_cache_fallback.jsonl \\
    --out output/clinic_services_price_audit.csv
"""

from __future__ import annotations

import argparse
import csv
import json
import os
import sys
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path
from typing import Any

import httpx

sys.path.insert(0, str(Path(__file__).resolve().parent))
from scrape_utils import (  # noqa: E402
    discover_scrape_path,
    load_cache_jsonl,
    normalize_url,
    scrape_one,
    url_key,
)

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from utils.service_price_verify_extract import decide_verdict, extract_for_service  # noqa: E402
from utils.service_price_verify_hits import ExtractResult, parse_decimal  # noqa: E402

# Bypass local HTTP proxies for LAN Firecrawl (common misconfig: http_proxy -> :7890).
for _k in (
    "http_proxy",
    "https_proxy",
    "HTTP_PROXY",
    "HTTPS_PROXY",
    "ALL_PROXY",
    "all_proxy",
):
    os.environ.pop(_k, None)
os.environ["NO_PROXY"] = "*"
os.environ["no_proxy"] = "*"

DEFAULT_BASE = "http://192.168.1.68:3002"
DEFAULT_KEY = os.environ.get("FIRECRAWL_API_KEY", "local-monitor-only-20260829")


def load_services(path: Path) -> list[dict[str, str]]:
    with path.open(newline="", encoding="utf-8") as f:
        return list(csv.DictReader(f))


def write_audit(path: Path, rows: list[dict[str, Any]]) -> None:
    fields = [
        "service_id",
        "business_id",
        "service_name",
        "unit_type",
        "source_url",
        "current_price",
        "proposed_price",
        "verdict",
        "needs_review",
        "evidence_normalized",
        "scrape_source",
        "notes",
    ]
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=fields)
        w.writeheader()
        for r in rows:
            w.writerow({k: r.get(k, "") for k in fields})


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--services-csv", type=Path, required=True)
    ap.add_argument("--out", type=Path, default=Path("output/clinic_services_price_audit.csv"))
    ap.add_argument("--cache-jsonl", type=Path, default=None)
    ap.add_argument("--scrape-dir", type=Path, default=Path("output/scrape_live_v2"))
    ap.add_argument("--base-url", default=DEFAULT_BASE)
    ap.add_argument("--api-key", default=DEFAULT_KEY)
    ap.add_argument("--concurrency", type=int, default=4)
    ap.add_argument(
        "--scrape-path",
        default="/v2/scrape",
        help="Firecrawl scrape path (default /v2/scrape with onlyCleanContent)",
    )
    ap.add_argument("--skip-scrape", action="store_true")
    ap.add_argument("--limit-urls", type=int, default=0, help="debug: scrape only N urls")
    ap.add_argument("--smoke-url", default="", help="if set, scrape one URL and exit")
    args = ap.parse_args()

    # Smoke-only mode
    if args.smoke_url:
        with httpx.Client(trust_env=False) as client:
            path = args.scrape_path or discover_scrape_path(client, args.base_url, args.api_key)
            res = scrape_one(client, args.base_url, path, args.api_key, args.smoke_url)
            print(
                json.dumps(
                    {
                        "path": path,
                        "ok": res.ok,
                        "md_len": len(res.markdown),
                        "price_text_len": len(res.price_text),
                        "containers": len(res.containers),
                        "err": res.error,
                        "preview": res.price_text[:300],
                    },
                    ensure_ascii=False,
                )
            )
            return 0 if res.ok else 1

    services = load_services(args.services_csv)
    cache = load_cache_jsonl(args.cache_jsonl)
    args.scrape_dir.mkdir(parents=True, exist_ok=True)

    # Distinct URLs
    urls: list[str] = []
    seen = set()
    for s in services:
        u = (s.get("source_url") or "").strip()
        if not u:
            continue
        nu = normalize_url(u)
        if nu in seen:
            continue
        seen.add(nu)
        urls.append(u)
    if args.limit_urls > 0:
        urls = urls[: args.limit_urls]

    scrape_map: dict[str, dict[str, Any]] = {}  # normalize_url -> {markdown, source, error}
    # Resume from prior live scrapes
    meta_path = args.scrape_dir / "_index.jsonl"
    if meta_path.exists():
        with meta_path.open(encoding="utf-8") as f:
            for line in f:
                if not line.strip():
                    continue
                obj = json.loads(line)
                scrape_map[normalize_url(obj["url"])] = obj

    to_fetch = [u for u in urls if normalize_url(u) not in scrape_map or not scrape_map[normalize_url(u)].get("markdown")]
    live_ok = live_fail = 0

    if not args.skip_scrape and to_fetch:
        print(f"Scraping {len(to_fetch)}/{len(urls)} URLs via {args.base_url} concurrency={args.concurrency}", flush=True)
        with httpx.Client(trust_env=False) as client:
            path = args.scrape_path or discover_scrape_path(client, args.base_url, args.api_key)
            print(f"Using scrape path {path} (onlyCleanContent={'yes' if path.startswith('/v2') else 'n/a'})", flush=True)

            def work(u: str) -> dict[str, Any]:
                sr = scrape_one(client, args.base_url, path, args.api_key, u)
                return {
                    "url": u,
                    "ok": sr.ok,
                    "markdown": sr.markdown,
                    "price_text": sr.price_text,
                    "containers": len(sr.containers),
                    "error": sr.error,
                    "source": "live" if sr.ok else "none",
                }

            with ThreadPoolExecutor(max_workers=max(1, args.concurrency)) as ex:
                futs = {ex.submit(work, u): u for u in to_fetch}
                done = 0
                with meta_path.open("a", encoding="utf-8") as meta_f:
                    for fut in as_completed(futs):
                        res = fut.result()
                        nu = normalize_url(res["url"])
                        scrape_map[nu] = res
                        # persist markdown file
                        if res["ok"]:
                            live_ok += 1
                            (args.scrape_dir / f"{url_key(res['url'])}.md").write_text(res["markdown"], encoding="utf-8")
                        else:
                            live_fail += 1
                        # store compact index line without full md if huge — keep md in map for this run
                        meta_f.write(
                            json.dumps(
                                {
                                    "url": res["url"],
                                    "ok": res["ok"],
                                    "error": res["error"],
                                    "source": res["source"],
                                    "md_len": len(res.get("markdown") or ""),
                                    "markdown": res.get("markdown") or "",
                                    "price_text": res.get("price_text") or "",
                                    "containers": res.get("containers") or 0,
                                },
                                ensure_ascii=False,
                            )
                            + "\n"
                        )
                        meta_f.flush()
                        done += 1
                        if done % 25 == 0 or done == len(to_fetch):
                            print(f"  progress {done}/{len(to_fetch)} ok={live_ok} fail={live_fail}", flush=True)
    else:
        # count from map
        for u in urls:
            obj = scrape_map.get(normalize_url(u)) or {}
            if obj.get("markdown"):
                live_ok += 1
            else:
                live_fail += 1

    # Resolve markdown per URL: live → cache
    resolved: dict[str, tuple[str, str, str]] = {}  # nu -> (md, price_text, scrape_source)
    cache_hits = 0
    for u in urls:
        nu = normalize_url(u)
        obj = scrape_map.get(nu) or {}
        md = obj.get("markdown") or ""
        if md.strip():
            resolved[nu] = (md, obj.get("price_text") or md, "live")
            continue
        # try loading from .md file
        md_path = args.scrape_dir / f"{url_key(u)}.md"
        if md_path.exists():
            md = md_path.read_text(encoding="utf-8")
            if md.strip():
                resolved[nu] = (md, obj.get("price_text") or md, "live")
                continue
        cmd = cache.get(nu) or cache.get(u) or ""
        if cmd.strip():
            resolved[nu] = (cmd, cmd, "cache")
            cache_hits += 1
        else:
            resolved[nu] = ("", "", "none")

    # Extract per service
    audit_rows: list[dict[str, Any]] = []
    verdict_counts: dict[str, int] = {}
    for s in services:
        u = (s.get("source_url") or "").strip()
        if not u:
            continue
        nu = normalize_url(u)
        md, price_text, scrape_source = resolved.get(nu, ("", "", "none"))
        current = parse_decimal(s.get("regular_price"))
        name = s.get("service_name") or ""
        # price_text (hidden-filtered top-level price divs) is primary evidence;
        # markdown remains the backup when no price containers were found.
        text_for_extraction = price_text or md
        extracted = (
            extract_for_service(text_for_extraction, name, u)
            if text_for_extraction
            else ExtractResult(None, "", "not_found", False, [])
        )
        verdict, proposed, needs_review, notes = decide_verdict(current, extracted, scrape_source)
        if extracted.evidence and verdict != "scrape_failed":
            # keep evidence only when we have page text
            evidence = extracted.evidence
        else:
            evidence = ""
        if scrape_source == "none":
            notes = (notes + "; " if notes else "") + (scrape_map.get(nu) or {}).get("error", "no markdown")
        row = {
            "service_id": s.get("service_id"),
            "business_id": s.get("business_id"),
            "service_name": name,
            "unit_type": s.get("unit_type"),
            "source_url": u,
            "current_price": str(current) if current is not None else "",
            "proposed_price": str(proposed) if proposed is not None else "",
            "verdict": verdict,
            "needs_review": "true" if needs_review else "false",
            "evidence_normalized": evidence,
            "scrape_source": scrape_source,
            "notes": notes,
        }
        audit_rows.append(row)
        verdict_counts[verdict] = verdict_counts.get(verdict, 0) + 1

    write_audit(args.out, audit_rows)

    summary = {
        "services": len(audit_rows),
        "distinct_urls": len(urls),
        "live_ok": live_ok,
        "live_fail": live_fail,
        "cache_fallback_used": cache_hits,
        "unresolved_urls": sum(1 for u in urls if resolved[normalize_url(u)][2] == "none"),
        "verdicts": verdict_counts,
        "out": str(args.out),
    }
    print(json.dumps(summary, indent=2, ensure_ascii=False))
    # Also write failed URL list for cache backfill
    failed_urls = [u for u in urls if resolved[normalize_url(u)][2] == "none"]
    fail_path = args.out.with_suffix(".failed_urls.txt")
    fail_path.write_text("\n".join(failed_urls) + ("\n" if failed_urls else ""), encoding="utf-8")
    # URLs that need cache (live failed but maybe cache exists remotely)
    need_cache = [
        u
        for u in urls
        if not ((scrape_map.get(normalize_url(u)) or {}).get("markdown") or "").strip()
        and normalize_url(u) not in {normalize_url(x) for x in []}
    ]
    # live-failed regardless of cache
    live_failed_urls = []
    for u in urls:
        obj = scrape_map.get(normalize_url(u)) or {}
        if not (obj.get("markdown") or "").strip():
            md_path = args.scrape_dir / f"{url_key(u)}.md"
            if not md_path.exists():
                live_failed_urls.append(u)
    (args.out.with_suffix(".live_failed_urls.txt")).write_text(
        "\n".join(live_failed_urls) + ("\n" if live_failed_urls else ""), encoding="utf-8"
    )
    print(f"live_failed_urls={len(live_failed_urls)} unresolved={len(failed_urls)}", flush=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())
