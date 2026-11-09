#!/usr/bin/env python3
"""Single entry point for the standard promotion / membership pipeline.

    python scripts/promo_pipeline.py <command> [command options]

Stages, in order:
  1 discover-businesses -> register-businesses   Google Maps scraper finds businesses
  2 discover-pages -> review-pages -> retry-pages -> render-shards -> (scripts/run_render_review_deep.sh)
    -> render-verdicts -> finalize-pages           promotion / membership URLs per business domain
  3 crawl                                          self-hosted Firecrawl into the local raw store
  4 extract-pages                                  raw -> clinic_promotions / clinic_memberships
  5 extract-offers                                 promotion_content / benefits -> offers + items
  6 link-services                                  offer items -> clinic_services + service_id
Maintenance: merge-promotions, deactivate-expired, monitor, quality, batch-gate.

Every command runs the script's own ``main()`` with the remaining arguments. No command connects to the
database: reads come from snapshot files exported through the Supabase MCP (``read-sql``), writes are rendered
to a single-transaction ``apply.sql`` that is executed through the Supabase MCP (``apply-sql``).
"""
from __future__ import annotations

import importlib
import sys
from pathlib import Path
from typing import Callable, Dict, List, Optional, Tuple

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

# command -> (module, one-line purpose, writes the database?)
COMMANDS: Dict[str, Tuple[str, str, str]] = {
    "select-city": ("scripts.select_next_city", "pick the next city to scan", "no"),
    "discover-businesses": ("scripts.gmaps_discovery", "1: Google Maps scraper places for a city", "no"),
    "register-businesses": ("scripts.process_discovery_results",
                            "1: link / insert discovered places into master_business_info", "--apply"),
    "topic-search": ("scripts.topic_search",
                     "2: topic search in a city -> clinics to register and seed pages for discover-pages", "no"),
    "discover-pages": ("scripts.discover_new_promo_pages",
                       "2: promotion / membership pages per business domain (Map + price evidence)", "no"),
    "review-pages": ("scripts.review_promo_candidates", "2: add / skip decisions for discovered pages", "no"),
    "retry-pages": ("scripts.retry_failed_pages", "2: retry pages that failed review", "no"),
    "render-shards": ("scripts.build_deep_review_shards", "2: queue unverified pages for the ego render review", "no"),
    "render-verdicts": ("scripts.deep_review_verdicts", "2: render-review verdicts (expanded text + OCR)", "no"),
    "finalize-pages": ("scripts.finalize_discovery_decisions", "2: final page list for crawling", "no"),
    "crawl": ("scripts.scrape_urls_to_raw", "3: self-hosted Firecrawl into the raw file store", "no (raw files)"),
    "recrawl-sources": ("scripts.recrawl_sources", "decisions file to re-crawl existing promotions / memberships",
                        "no"),
    "visibility-manifest": ("scripts.visibility_manifest",
                            "render-verify extract-pages content lines in ego-lite and build the visibility manifest",
                            "no"),
    "render-triage": ("scripts.render_triage",
                      "route each planned promotion to reject / render / markdown_traceable before the render review",
                      "no"),
    "extract-pages": ("scripts.extract_pages", "4: raw -> clinic_promotions / clinic_memberships",
                      "apply-sql via MCP"),
    "extract-offers": ("scripts.extract_offers", "5: promotion_content / benefits -> offers and items",
                       "apply-sql via MCP"),
    "link-services": ("scripts.link_services", "6: identify services, backfill clinic_services and service_id",
                      "apply-sql via MCP"),
    "merge-promotions": ("scripts.merge_promotions",
                         "merge duplicate promotions per (business_id, source URL key) and fold hub child pages",
                         "apply-sql via MCP"),
    "enforce-promotion-rules": ("scripts.enforce_promotion_rules",
                                "deactivate existing active promotions that break the write rules", "apply-sql via MCP"),
    "clear-membership-benefits": ("scripts.clear_membership_benefits",
                                  "set unverified membership benefits to NULL (unknown)", "apply-sql via MCP"),
    "split-offers": ("scripts.split_offers", "split an offer that lists several services under one price into "
                     "independent offers", "apply-sql via MCP"),
    "rename-items": ("scripts.rename_items", "rewrite problematic item_name values of active offers",
                     "apply-sql via MCP"),
    "upload-sheet-memberships": ("scripts.upload_sheet_memberships",
                                 "write QA-passed membership rows of the upload sheet into clinic_memberships",
                                 "apply-sql via MCP"),
    "delete-offer-shells": ("scripts.delete_offer_shells",
                            "delete inactive offers without items or user references (gate I)", "apply-sql via MCP"),
    "deactivate-expired": ("scripts.deactivate_expired", "daily: deactivate promotions past campaign_end_date",
                           "apply.sql via MCP"),
    "monitor": ("scripts.run_monitor_price_sync",
                "daily: re-crawl promotion pages due by their 7/15/30-day poll tier and sync changes",
                "control file"),
    "monitor-schedule": ("scripts.monitor_schedule", "monitor poll tiers and pages due now", "no"),
    "notify-run": ("scripts.notify_monitor_run", "Feishu summary of the last monitor run (server launchers)", "no"),
    "quality": ("scripts.run_data_quality_audit",
                "data-quality audit and rendered recheck", "--apply (with an approved plan)"),
    "batch-gate": ("scripts.run_batch_gate_checks", "batch gate checks A-J (read-only)", "no"),
    "crawl-schedule": ("scripts.crawl_scheduler", "plan due businesses for the scheduled crawl", "state files"),
    "crawl-record": ("scripts.crawl_record_from_monitor", "record scheduled crawl results", "state files"),
    "read-snapshot": ("scripts.read_snapshot",
                      "run a command's read.sql with the Supabase CLI and save snapshot.json", "no (read only)"),
    "pending-sql": ("scripts.pending_sql",
                    "--pull pending SQL files from the monitor host; --mark-applied FILE after MCP execution "
                    "(locally and on the monitor host)",
                    "no (moves files)"),
}


def _usage() -> str:
    width = max(len(name) for name in COMMANDS)
    rows = [f"  {name:<{width}}  {purpose}  [writes: {writes}]" for name, (_, purpose, writes) in COMMANDS.items()]
    return "usage: promo_pipeline.py <command> [options]\n\ncommands:\n" + "\n".join(rows) + "\n"


def main(argv: Optional[List[str]] = None, loader: Callable[[str], object] = importlib.import_module) -> int:
    args = list(sys.argv[1:] if argv is None else argv)
    if not args or args[0] in {"-h", "--help", "help"}:
        sys.stdout.write(_usage())
        return 0
    command, rest = args[0], args[1:]
    if command not in COMMANDS:
        sys.stderr.write(f"unknown command: {command}\n\n{_usage()}")
        return 2
    module = loader(COMMANDS[command][0])
    previous = sys.argv
    sys.argv = [f"promo_pipeline.py {command}", *rest]
    try:
        result = module.main()  # type: ignore[attr-defined]
    finally:
        sys.argv = previous
    return int(result) if isinstance(result, int) else 0


if __name__ == "__main__":
    raise SystemExit(main())
