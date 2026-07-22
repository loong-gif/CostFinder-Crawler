from __future__ import annotations

import asyncio
import sys
from pathlib import Path
from typing import Any, Dict, List

import pytest

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from crawler.promo_site_crawler import PromoSiteCrawler, SiteTarget
from crawler.staging_recrawl import SupabaseRestClient, SyncTarget, build_sync_target_for_domain, recrawl_domain_via_firecrawl
from utils.url_safety import assert_safe_crawl_entry_url, crawl_entry_url_error


@pytest.mark.parametrize(
    "url",
    [
        "https://example.com",
        "https://example.com/pricing?q=1",
        "http://example.org/path",
        "http://8.8.8.8/status",
        "https://sub.example.com/deals",
    ],
)
def test_crawl_entry_url_error_allows_public_urls(url: str):
    assert crawl_entry_url_error(url) is None
    assert assert_safe_crawl_entry_url(url) == url


@pytest.mark.parametrize(
    "url,reason_fragment",
    [
        ("", "empty url"),
        ("https:///pricing", "empty host"),
        ("file:///etc/passwd", "unsupported scheme"),
        ("ftp://example.com/", "unsupported scheme"),
        ("https://user:pass@example.com/", "userinfo"),
        ("http://localhost/", "localhost"),
        ("https://LOCALHOST/pricing", "localhost"),
        ("https://printer.local/", ".local"),
        ("http://127.0.0.1/", "blocked ip"),
        ("http://192.168.0.1/", "blocked ip"),
        ("http://10.0.0.1/", "blocked ip"),
        ("http://172.16.0.1/", "blocked ip"),
        ("http://169.254.169.254/", "blocked ip"),
        ("http://0.0.0.0/", "blocked ip"),
        ("http://[::1]/", "blocked ip"),
        ("http://[fe80::1]/", "blocked ip"),
        ("http://224.0.0.1/", "blocked ip"),
    ],
)
def test_crawl_entry_url_error_rejects_unsafe_urls(url: str, reason_fragment: str):
    reason = crawl_entry_url_error(url)
    assert reason is not None
    assert reason_fragment in reason
    with pytest.raises(ValueError, match=reason_fragment):
        assert_safe_crawl_entry_url(url)


class _RecordingFetchEngine:
    engine_name = "firecrawl"

    def __init__(self) -> None:
        self.fetch_calls: List[str] = []

    async def start(self) -> None:
        return None

    async def close(self) -> None:
        return None

    async def fetch(self, url: str):
        self.fetch_calls.append(url)
        raise AssertionError("fetch must not be called for unsafe entry URL")


def test_crawl_site_rejects_unsafe_entry_without_fetch():
    engine = _RecordingFetchEngine()
    crawler = PromoSiteCrawler(fetch_engine=engine, max_candidate_pages=3)
    site = SiteTarget(
        master_id=1,
        business_id=2,
        name="Loopback Clinic",
        website="http://127.0.0.1/",
        website_clean="127.0.0.1",
        process_flag="",
        domain_name="127.0.0.1",
    )

    async def _run():
        hits, stats = await crawler.crawl_site(site)
        assert hits == []
        assert stats["site_failed"] == 1
        assert stats["site_success"] == 0
        assert engine.fetch_calls == []

    asyncio.run(_run())


class _FakeSupabaseClient(SupabaseRestClient):
    def __init__(self, master_rows: List[Dict[str, Any]], promo_rows: List[Dict[str, Any]] | None = None):
        self._master_rows = master_rows
        self._promo_rows = promo_rows or []

    def fetch_rows(
        self,
        table: str,
        select: str,
        *,
        filters: Dict[str, str] | None = None,
        limit: int | None = None,
        offset: int | None = None,
        order: str | None = None,
    ) -> List[Dict[str, Any]]:
        if table == "master_business_info":
            return self._master_rows
        if table == "promo_website_staging":
            return self._promo_rows
        return []


def test_build_sync_target_for_domain_rejects_unsafe_website_url():
    client = _FakeSupabaseClient(
        master_rows=[
            {
                "id": 1,
                "business_id": 10,
                "name": "Localhost Spa",
                "website": "http://localhost/",
                "website_clean": "localhost",
                "process_flag": "",
            }
        ]
    )

    with pytest.raises(ValueError, match="Unsafe crawl entry URL"):
        build_sync_target_for_domain(client, "localhost")


def test_recrawl_domain_via_firecrawl_rejects_unsafe_target_before_firecrawl(monkeypatch):
    target = SyncTarget(
        domain_name="evil.local",
        website_url="https://evil.local/",
        name="Evil",
        master_id=None,
        business_id=None,
    )

    def _fake_build_sync_target(_client, _domain):
        return target

    crawl_called = {"value": False}

    class _FakeFirecrawl:
        def crawl(self, *args, **kwargs):
            crawl_called["value"] = True
            raise AssertionError("Firecrawl crawl must not run for unsafe URL")

    monkeypatch.setattr(
        "crawler.staging_recrawl.build_sync_target_for_domain",
        _fake_build_sync_target,
    )
    monkeypatch.setattr(
        "crawler.staging_recrawl.get_firecrawl_client",
        lambda: _FakeFirecrawl(),
    )

    with pytest.raises(ValueError, match="Unsafe crawl entry URL|\\.local"):
        recrawl_domain_via_firecrawl("evil.local", client=_FakeSupabaseClient([]))

    assert crawl_called["value"] is False
