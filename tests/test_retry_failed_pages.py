from __future__ import annotations

import json

from scripts.retry_failed_pages import collect_failed, retry_one

FILLER = "Our medspa offers a full range of aesthetic services for every patient in town. " * 6


def _html(body: str) -> str:
    return f"<html><body><main>{FILLER}{body}</main></body></html>"


def _write(tmp_path, name, businesses):
    path = tmp_path / name
    path.write_text(json.dumps({"businesses": businesses}))
    return path


def test_collect_failed_dedupes_and_drops_urls_that_later_succeeded(tmp_path) -> None:
    a = _write(tmp_path, "a.json", [{"business_id": 1, "name": "S", "city": "X", "website": "https://s.com", "pages": [
        {"url": "https://s.com/a", "status": "scrape_failed", "sources": ["map"], "kind_hint": "promotion", "error": "500"},
        {"url": "https://s.com/b", "status": "scrape_failed", "sources": ["map"], "kind_hint": "membership"}]}])
    b = _write(tmp_path, "b.json", [{"business_id": 1, "name": "S", "city": "X", "website": "https://s.com", "pages": [
        {"url": "https://s.com/a", "status": "scrape_failed", "sources": ["search"], "title": "A"},
        {"url": "https://s.com/b", "status": "valid", "sources": ["search"]}]}])
    got = collect_failed([a, b])
    assert list(got) == ["https://s.com/a"]  # /b succeeded in the second run
    assert got["https://s.com/a"]["sources"] == ["map", "search"]
    assert [b["business_id"] for b in got["https://s.com/a"]["businesses"]] == [1]


def test_a_chain_page_is_retried_once_for_every_branch_that_failed(tmp_path) -> None:
    page = {"url": "https://chain.com/specials", "status": "scrape_failed", "sources": ["map"], "kind_hint": "promotion"}
    a = _write(tmp_path, "a.json", [
        {"business_id": 1, "name": "Chain WeHo", "city": "West Hollywood", "website": "https://chain.com", "pages": [page]},
        {"business_id": 2, "name": "Chain Santa Monica", "city": "Santa Monica", "website": "https://chain.com",
         "pages": [page]}])
    got = collect_failed([a])
    assert list(got) == ["https://chain.com/specials"]
    assert [b["business_id"] for b in got["https://chain.com/specials"]["businesses"]] == [1, 2]


class _Fc:
    def __init__(self, outcomes):
        self.outcomes, self.calls = list(outcomes), 0

    def scrape(self, url):
        self.calls += 1
        outcome = self.outcomes.pop(0)
        if isinstance(outcome, Exception):
            raise outcome
        return outcome


RECORD = {"business_id": 1, "kind_hint": "promotion", "sources": ["search"], "title": "T", "hop": 0, "via": None}
GOOD = {"markdown": "Botox $11 per unit this month", "rawHtml": _html("<p>Botox $11 per unit this month</p>")}


def test_retry_recovers_after_transient_500s_with_backoff() -> None:
    waits = []
    fc = _Fc([RuntimeError("500"), RuntimeError("500"), GOOD])
    page = retry_one(fc, "https://s.com/a", RECORD, attempts=3, backoff=5, sleep=waits.append,
                     fetch=lambda u: (_ for _ in ()).throw(AssertionError("fallback not needed")))
    assert page["status"] in ("valid", "needs_review") and page["engine"] == "firecrawl" and page["retried"]
    assert waits == [5, 15]  # exponential backoff between attempts, none after the last


def test_retry_falls_back_to_direct_fetch_then_gives_up_cleanly() -> None:
    fc = _Fc([RuntimeError("500")] * 2)
    page = retry_one(fc, "https://s.com/a", RECORD, attempts=2, backoff=0, sleep=lambda s: None, fetch=lambda u: GOOD)
    assert page["engine"] == "direct_http" and page["status"] in ("valid", "needs_review")

    fc = _Fc([RuntimeError("500")] * 2)

    def broken(url):
        raise ValueError("unsupported_content_type")

    page = retry_one(fc, "https://s.com/a", RECORD, attempts=2, backoff=0, sleep=lambda s: None, fetch=broken)
    assert page["status"] == "scrape_failed" and "direct: ValueError" in page["error"]
