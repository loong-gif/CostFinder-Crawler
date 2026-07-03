"""Self-check: Firecrawl client honors FIRECRAWL_API_URL when set."""
from __future__ import annotations

import os
import sys
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))


def test_monitor_client_uses_api_url():
    captured = {}

    class FakeFirecrawl:
        def __init__(self, **kwargs):
            captured.update(kwargs)

    with patch.dict(os.environ, {"FIRECRAWL_API_KEY": "k", "FIRECRAWL_API_URL": "http://72.52.161.65:3002/"}, clear=False):
        with patch("firecrawl.Firecrawl", FakeFirecrawl):
            from scripts.firecrawl_monitor import get_firecrawl_client

            get_firecrawl_client()
    assert captured["api_url"] == "http://72.52.161.65:3002"
    assert captured["api_key"] == "k"


def test_monitor_client_omits_api_url_when_unset():
    captured = {}

    class FakeFirecrawl:
        def __init__(self, **kwargs):
            captured.update(kwargs)

    env = {k: v for k, v in os.environ.items() if k != "FIRECRAWL_API_URL"}
    with patch.dict(os.environ, {**env, "FIRECRAWL_API_KEY": "k"}, clear=True):
        with patch("firecrawl.Firecrawl", FakeFirecrawl):
            from scripts.firecrawl_monitor import get_firecrawl_client

            get_firecrawl_client()
    assert "api_url" not in captured


if __name__ == "__main__":
    test_monitor_client_uses_api_url()
    test_monitor_client_omits_api_url_when_unset()
    print("ok")
