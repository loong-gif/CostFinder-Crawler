import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "one-off"))

from candidates_20261020_parallel import discovery_row_status, prepare_row_for_run, should_retry_discovery


def test_pass_and_scrape_failed_stays_crawl_failed():
    result = {
        "pages": [
            {"url": "https://a.com/specials", "status": "valid"},
            {"url": "https://a.com/deals", "status": "scrape_failed"},
        ],
        "search_status": "ok",
    }
    assert discovery_row_status(result) == ("crawl_failed", "部分页面抓取失败")


def test_search_down_but_pages_screened():
    result = {
        "pages": [{"url": "https://a.com/specials", "status": "rejected", "reasons": ["no_discount_or_price_evidence"]}],
        "search_status": "skipped_engines_down",
    }
    assert discovery_row_status(result) == ("no_promotion_page", "现有发现流程未找到包含有效价格或优惠证据的页面")


def test_search_down_with_valid_page():
    result = {
        "pages": [{"url": "https://a.com/specials", "status": "valid"}],
        "search_status": "error",
        "search_error": "Timeout",
    }
    assert discovery_row_status(result) == ("pending_extraction", "等待结构化抽取与当前可见性核验")


def test_prepare_reconciles_old_search_failure():
    row = {
        "status": "crawl_failed",
        "reason": "部分页面抓取或搜索失败",
        "discovery_completed_at": "2026-10-21T00:00:00+00:00",
        "discovery": {
            "pages": [{"url": "https://a.com/member", "status": "needs_review"}],
            "search_status": "skipped_engines_down",
        },
    }
    assert prepare_row_for_run(row) == "done"
    assert row["status"] == "pending_extraction"


def test_should_retry_partial_scrape():
    row = {
        "status": "crawl_failed",
        "discovery_completed_at": "t",
        "discovery": {"pages": [{"status": "scrape_failed", "url": "https://a.com/x"}]},
    }
    assert should_retry_discovery(row) is True
