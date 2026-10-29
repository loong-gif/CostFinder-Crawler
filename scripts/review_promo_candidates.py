#!/usr/bin/env python3
"""Review discovery results, decide which pages join the monitor, and (optionally) add them.

  python scripts/review_promo_candidates.py --input state/discovery/new_promo_all.json [--input ...]
        [--write-monitor-config PATH] [--merge-config]

Always writes ``state/discovery/monitor_decisions.json``. ``--write-monitor-config`` writes a
monitor-style candidate config; ``--merge-config`` also appends the URLs to
``config/firecrawl_monitor_migration.json`` as ``discovered_NN`` monitors (idempotent). Nothing is
deployed and the database is only read.
"""
from __future__ import annotations

import argparse
import collections
import json
import sys
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Any, Dict, List

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from utils.firecrawl_scrape_raw_db import canonical_scrape_url  # noqa: E402
from utils.promo_value_review import BusinessContext, decide  # noqa: E402
from utils.supabase_access import fetch_all_rows, load_supabase_client  # noqa: E402

CONFIG = PROJECT_ROOT / "config" / "firecrawl_monitor_migration.json"
BUCKET = 50


def load_businesses() -> Dict[int, BusinessContext]:
    client = load_supabase_client(PROJECT_ROOT, role="service")
    master = fetch_all_rows(client, "master_business_info", "business_id,name,city,review_count,score,website",
                            order="business_id.asc")
    promos = fetch_all_rows(client, "clinic_promotions", "promotion_id,business_id,is_active", order="promotion_id.asc")
    touched: set = set()
    active: set = set()
    for row in promos:
        if row.get("business_id") is not None:
            touched.add(row["business_id"])
            if row.get("is_active"):
                active.add(row["business_id"])
    for table, pk in (("clinic_memberships", "plan_id"), ("clinic_services", "service_id"),
                      ("promo_offer_master", "id")):
        for row in fetch_all_rows(client, table, f"{pk},business_id", order=f"{pk}.asc"):
            if row.get("business_id") is not None:
                touched.add(row["business_id"])
                if table == "clinic_memberships":
                    active.add(row["business_id"])
    return {
        r["business_id"]: BusinessContext(
            business_id=r["business_id"], name=r.get("name") or "", city=r.get("city") or "",
            review_count=r.get("review_count"), score=r.get("score"), website=r.get("website") or "",
            has_active_offer=r["business_id"] in active, never_processed=r["business_id"] not in touched)
        for r in master
    }


def load_pages(paths: List[Path]) -> List[Dict[str, Any]]:
    pages: List[Dict[str, Any]] = []
    for path in paths:
        data = json.loads(path.read_text(encoding="utf-8"))
        for business in data.get("businesses", []):
            for page in business.get("pages", []):
                pages.append({**page, "business_id": business["business_id"]})
    return pages


def chunk(urls: List[str], size: int = BUCKET) -> List[List[str]]:
    return [urls[i:i + size] for i in range(0, len(urls), size)]


def merge_into_config(urls: List[str], config_path: Path = CONFIG) -> Dict[str, int]:
    """Append ``urls`` to ``discovered_NN`` monitors, skipping URLs the config already contains."""
    config = json.loads(config_path.read_text(encoding="utf-8"))
    monitors = config["monitors"]
    present = {canonical_scrape_url(u) for m in monitors.values() for t in m.get("targets", []) for u in t.get("urls", [])}
    fresh = [u for u in dict.fromkeys(urls) if canonical_scrape_url(u) not in present]
    template = monitors[sorted(monitors)[-1]]
    existing = sorted(k for k in monitors if k.startswith("discovered_"))
    added = 0
    for key in existing:
        group = monitors[key]["targets"][0]["urls"]
        while fresh and len(group) < BUCKET:
            group.append(fresh.pop(0))
            added += 1
    number = len(existing)
    while fresh:
        number += 1
        key = f"discovered_{number:02d}"
        batch, fresh = fresh[:BUCKET], fresh[BUCKET:]
        monitors[key] = {
            "name": f"costfinder_clinic_promotions_{key}",
            "schedule": template.get("schedule"),
            "status": "active",
            "targets": [{"id": f"{key}_scrape", "type": "scrape", "urls": batch}],
        }
        added += len(batch)
    if added:
        config_path.write_text(json.dumps(config, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return {"added": added, "already_present": len(urls) - added}


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--input", type=Path, action="append", required=True)
    parser.add_argument("--out", type=Path, default=PROJECT_ROOT / "state" / "discovery" / "monitor_decisions.json")
    parser.add_argument("--write-monitor-config", type=Path, nargs="?", const=Path(
        PROJECT_ROOT / "state" / "crawl_schedule" / "promo_discovery_monitor.json"))
    parser.add_argument("--merge-config", action="store_true")
    parser.add_argument("--today", help="override today's date (YYYY-MM-DD) for tests")
    args = parser.parse_args()

    today = date.fromisoformat(args.today) if args.today else date.today()
    decisions = decide(load_pages(args.input), load_businesses(), today)
    counts = collections.Counter(d["decision"] for d in decisions)
    reasons = collections.Counter(d["reasons"][-1] for d in decisions if d["decision"] == "skip")
    selected = [d["url"] for d in decisions if d["decision"] in ("add", "add_with_review")]
    summary = {"pages": len(decisions), **counts, "skip_reasons": dict(reasons.most_common(8)),
               "businesses_with_additions": len({d["business_id"] for d in decisions if d["url"] in set(selected)})}
    args.out.parent.mkdir(parents=True, exist_ok=True)
    args.out.write_text(json.dumps({"generated_at": datetime.now(timezone.utc).isoformat(), "summary": summary,
                                    "decisions": decisions}, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(json.dumps(summary, ensure_ascii=False))
    if args.write_monitor_config:
        monitors = {}
        for i, urls in enumerate(chunk(selected), start=1):
            name = f"costfinder_discovered_{today.strftime('%Y%m%d')}_{i:02d}"
            monitors[name] = {"name": name, "status": "active", "targets": [{"id": f"{name}_scrape", "type": "scrape", "urls": urls}]}
        args.write_monitor_config.parent.mkdir(parents=True, exist_ok=True)
        args.write_monitor_config.write_text(json.dumps({"schema_version": 1, "monitors": monitors}, indent=2) + "\n")
        print(f"wrote {args.write_monitor_config} ({len(selected)} urls)")
    if args.merge_config:
        print("merged into config:", json.dumps(merge_into_config(selected)))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
