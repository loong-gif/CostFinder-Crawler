"""Smoke check for FirecrawlFetchEngine against a live Firecrawl instance."""
from __future__ import annotations

import asyncio
import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from crawler.fetch_engine import FirecrawlFetchEngine, FetchedPage


def test_firecrawl_fetch_engine_smoke():
    if not os.getenv("FIRECRAWL_API_KEY"):
        print("skip: FIRECRAWL_API_KEY not set")
        return

    async def _run():
        engine = FirecrawlFetchEngine()
        page = await engine.fetch("https://example.com")
        assert isinstance(page, FetchedPage)
        assert page.source_type == "markdown"
        assert page.content.strip()

    asyncio.run(_run())


if __name__ == "__main__":
    test_firecrawl_fetch_engine_smoke()
    print("ok")
