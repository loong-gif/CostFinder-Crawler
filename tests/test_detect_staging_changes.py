import importlib.util
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "scripts" / "detect_promo_website_staging_changes.py"
spec = importlib.util.spec_from_file_location("detect_staging_changes", SCRIPT)
mod = importlib.util.module_from_spec(spec)
assert spec.loader is not None
spec.loader.exec_module(mod)
build_row_result = mod.build_row_result


def test_build_row_result_marks_unchanged():
    row = {
        "promo_website_id": 1,
        "subpage_url": "https://example.com/pricing",
        "domain_name": "example.com",
        "page_content": "Botox $199",
    }
    crawl = {"success": True, "page_content": "Botox $199", "error_message": ""}
    result = build_row_result(row, crawl)
    assert result["change_type"] == "unchanged"
    assert result["needs_review"] is False


def test_build_row_result_flags_price_signal_lost():
    row = {
        "promo_website_id": 2,
        "subpage_url": "https://example.com/specials",
        "domain_name": "example.com",
        "page_content": "Botox $199 special",
    }
    crawl = {"success": True, "page_content": "Botox — call our office today", "error_message": ""}
    result = build_row_result(row, crawl)
    assert result["change_type"] == "changed"
    assert result["price_signal_lost"] is True
    assert result["needs_review"] is True


def test_build_row_result_handles_crawl_failure():
    row = {"promo_website_id": 3, "subpage_url": "https://example.com/x", "page_content": "x"}
    crawl = {"success": False, "page_content": "", "error_message": "timeout"}
    result = build_row_result(row, crawl)
    assert result["change_type"] == "crawl_failed"
    assert result["needs_review"] is True
