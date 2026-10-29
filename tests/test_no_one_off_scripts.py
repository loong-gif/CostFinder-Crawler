from __future__ import annotations

import re
from pathlib import Path

from scripts import promo_pipeline

ROOT = Path(__file__).resolve().parents[1]

# 标准流程建立前已存在、未接入流程入口的脚本（冻结名单，只减不增）。
# 它们经 REST、connect_writer 或 monitor 写入时被流程入口守卫拒绝；apply_sql_migration 是迁移工具，直接连接数据库。
LEGACY_SCRIPTS = frozenset({
    "analyze_all_monitors", "apply_extraction_repairs", "apply_pipeline_bundle", "apply_sql_migration",
    "audit_extraction_quality", "audit_missing_business_promotions_firecrawl", "audit_p0_p1_acceptance",
    "audit_promo_offer_master", "audit_promo_offer_prices", "audit_promotion_consistency",
    "backfill_clinic_promotions_firecrawl_gemini", "backfill_clinic_services_from_offers",
    "backfill_missing_scrape_links", "backfill_offer_fingerprints", "backfill_promo_offer_conditions",
    "backfill_promo_offer_regular_price", "backfill_promotion_screenshot_ocr", "build_promo_mcp_batch",
    "build_service_mapping_repair_dryrun", "clear_inconsistent_promo_offer_prices", "crawl_report",
    "deploy_monitor_price_sync", "discover_clinic_promotions_firecrawl", "dry_run_discovered_promotions",
    "evaluate_monitor_images", "execute_promo_offer_price_semantics", "export_monitor_price_sync",
    "export_promo_offer_master_qa", "extract_clinic_promotions_from_scrape_raw",
    "extract_clinic_services_from_search_raw", "extract_scrape_page_pipeline",
    "fill_promotion_screenshots_firecrawl", "fill_promotion_screenshots", "firecrawl_self_hosted_monitor",
    "hermes_notification_worker", "import_promo_offer_csv", "ocr_unverified_images",
    "render_clinic_services_mcp_sql", "render_clinic_services_restore_mcp_sql",
    "repair_clinic_memberships_full_table", "repair_clinic_services_quality", "repair_promo_offer_item_prices",
    "repair_promo_offer_prices", "rescrape_firecrawl_raw_only_main", "rescrape_ocr_raw",
    "retry_membership_dry_run", "run_clinic_services_firecrawl", "run_domain_architecture_pipeline",
    "run_monitor_image_evaluation", "sag_multi_retrieval_eval", "sag_persistent_pilot", "scrape_utils_check",
    "scrape_utils", "seed_clinic_services_botox", "seed_clinic_services_search", "seed_crawl_schedule",
    "summarize_unverified_render", "sync_replica_catalog", "sync_storage_to_replica", "verify_clinic_service_prices",
})


def test_one_off_directory_does_not_exist():
    assert not (ROOT / "one-off").exists(), "一次性脚本已归档到 archive/one-off；新逻辑写进流程命令"


def test_every_new_script_is_a_pipeline_command():
    registered = {module.rsplit(".", 1)[1] for module, _, _ in promo_pipeline.COMMANDS.values()}
    scripts = {path.stem for path in (ROOT / "scripts").glob("*.py")} - {"promo_pipeline", "__init__"}
    unregistered = sorted(scripts - registered - LEGACY_SCRIPTS)
    assert not unregistered, f"scripts 必须接入 scripts/promo_pipeline.py 的 COMMANDS: {unregistered}"


def test_no_dated_files_in_code_directories():
    dated = sorted(str(p.relative_to(ROOT)) for d in ("scripts", "utils", "tests")
                   for p in (ROOT / d).rglob("*") if p.is_file() and "__pycache__" not in p.parts
                   and re.search(r"(?<!\d)20\d{6}(?!\d)", p.name))
    assert not dated, f"带日期的文件名是一次性脚本的标志: {dated}"
