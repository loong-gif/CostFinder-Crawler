from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

import pytest

from scripts import firecrawl_self_hosted_monitor as monitor
from utils.monitor_price_store import digest

NOW = datetime(2026, 10, 15, tzinfo=timezone.utc)
FILLER = "Our medspa offers a full range of aesthetic services for every patient. " * 5
MARKDOWN = "Botox $12 per unit this month"
CLEAN_HTML = f"<html><body><main>{FILLER}<p>{MARKDOWN}</p></main></body></html>"


@dataclass
class _Page:
    status: str
    markdown: str
    facts: tuple
    response: Any


def _previous(tmp_path: Path, *, age_days: int = 3, content: str = MARKDOWN, accessible: bool = True) -> dict:
    (tmp_path / "evidence").mkdir(exist_ok=True)
    rendered = {"browser": {"accessible": accessible, "visual_digest": "v1", "context_digest": "c1"},
                "browser_path": "/old/browser.json"}
    (tmp_path / "evidence" / "art1.json").write_text(json.dumps({"rendered": rendered}))
    return {"artifact_id": "art1", "content_digest": digest(content),
            "rendered_at": (NOW - timedelta(days=age_days)).isoformat()}


def _page(html: str = CLEAN_HTML, markdown: str = MARKDOWN, status: str = "same") -> _Page:
    return _Page(status, markdown, ("Botox $12 per unit",), {"data": {"rawHtml": html}})


def test_mode_defaults_to_always(monkeypatch) -> None:
    monkeypatch.delenv("MONITOR_RENDER_MODE", raising=False)
    assert monitor.crawler_first_enabled() is False
    monkeypatch.setenv("MONITOR_RENDER_MODE", "crawler_first")
    assert monitor.crawler_first_enabled() is True


def test_unchanged_clean_page_reuses_previous_render(tmp_path) -> None:
    reuse = monitor.reusable_render("https://a.com/p", _page(), _previous(tmp_path), tmp_path, NOW.isoformat())
    assert reuse is not None
    assert reuse["reused_from_artifact"] == "art1" and reuse["browser"]["visual_digest"] == "v1"
    assert reuse["visibility_source"] == "crawler_html_filtered"


@pytest.mark.parametrize("case", ["changed", "stale", "hidden_claim", "empty_html", "no_prior_render", "error"])
def test_everything_else_goes_to_the_browser(tmp_path, case) -> None:
    previous = _previous(tmp_path)
    page = _page()
    if case == "changed":
        previous = _previous(tmp_path, content="Botox $15 per unit")
    elif case == "stale":
        previous = _previous(tmp_path, age_days=30)
    elif case == "hidden_claim":
        page = _page(html=f'<html><body>{FILLER}<p style="display:none">{MARKDOWN}</p></body></html>')
    elif case == "empty_html":
        page = _page(html="")
    elif case == "no_prior_render":
        previous = {k: v for k, v in previous.items() if k != "rendered_at"}
    elif case == "error":
        page = _page(status="error")
    assert monitor.reusable_render("https://a.com/p", page, previous, tmp_path, NOW.isoformat()) is None


def test_inaccessible_previous_render_is_not_reused(tmp_path) -> None:
    previous = _previous(tmp_path, accessible=False)
    assert monitor.reusable_render("https://a.com/p", _page(), previous, tmp_path, NOW.isoformat()) is None


def test_claim_hidden_by_the_site_stylesheet_goes_to_the_browser(tmp_path, css_site) -> None:
    css_site.add("site.css", ".was{display:none}")
    html = (f'<html><head><link rel="stylesheet" href="site.css"></head><body><main>{FILLER}'
            f'<p class="was">{MARKDOWN}</p></main></body></html>')
    url = css_site.add("p.html", html, content_type="text/html")
    assert monitor.reusable_render(url, _page(html=html), _previous(tmp_path), tmp_path, NOW.isoformat()) is None
    shown = html.replace('class="was"', 'class="now"')
    assert monitor.reusable_render(url, _page(html=shown), _previous(tmp_path), tmp_path, NOW.isoformat()) is not None
