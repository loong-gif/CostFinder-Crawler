"""Tests for Firecrawl search client guard."""
import os

import pytest

from utils import firecrawl_client


def test_search_client_requires_cloud_key_when_self_hosted(monkeypatch):
    monkeypatch.setenv("FIRECRAWL_API_URL", "http://72.52.161.65:3002")
    monkeypatch.setenv("FIRECRAWL_API_KEY", "self-hosted")
    monkeypatch.delenv("FIRECRAWL_SEARCH_API_KEY", raising=False)
    firecrawl_client.load_firecrawl_env()
    with pytest.raises(RuntimeError, match="FIRECRAWL_SEARCH_API_KEY"):
        firecrawl_client.get_firecrawl_search_client()
