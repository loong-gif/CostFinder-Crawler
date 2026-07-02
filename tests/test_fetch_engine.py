from __future__ import annotations

import pytest

from crawler.fetch_engine import JinaFetchEngine, create_fetch_engine
from crawler.promo_site_crawler import PromoSiteCrawler


def test_create_fetch_engine_defaults_to_jina():
    engine = create_fetch_engine()

    assert isinstance(engine, JinaFetchEngine)
    assert engine.engine_name == "jina"


def test_create_fetch_engine_rejects_non_jina_engine():
    with pytest.raises(ValueError, match="Unsupported fetch engine"):
        create_fetch_engine("playwright")


def test_promo_site_crawler_defaults_to_jina_engine():
    crawler = PromoSiteCrawler()

    assert crawler.engine_name == "jina"
    assert isinstance(crawler.fetch_engine, JinaFetchEngine)
