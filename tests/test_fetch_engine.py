from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from crawler.fetch_engine import FirecrawlFetchEngine, create_fetch_engine
from crawler.promo_site_crawler import PromoSiteCrawler


def test_create_fetch_engine_defaults_to_firecrawl():
    engine = create_fetch_engine()
    assert isinstance(engine, FirecrawlFetchEngine)
    assert engine.engine_name == "firecrawl"


def test_create_fetch_engine_rejects_non_firecrawl_engine():
    try:
        create_fetch_engine("playwright")
    except ValueError as exc:
        assert "Unsupported fetch engine" in str(exc)
    else:
        raise AssertionError("expected ValueError")


def test_promo_site_crawler_defaults_to_firecrawl_engine():
    crawler = PromoSiteCrawler()
    assert crawler.engine_name == "firecrawl"
    assert isinstance(crawler.fetch_engine, FirecrawlFetchEngine)


if __name__ == "__main__":
    test_create_fetch_engine_defaults_to_firecrawl()
    test_create_fetch_engine_rejects_non_firecrawl_engine()
    test_promo_site_crawler_defaults_to_firecrawl_engine()
    print("ok")
