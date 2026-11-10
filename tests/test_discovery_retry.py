from __future__ import annotations

from utils.discovery_retry import collect_failed, retry_failed_pages, retry_one

FILLER = "Our medspa offers a full range of aesthetic services for every patient in town. " * 6


def _html(body: str) -> str:
    return f"<html><body><main>{FILLER}{body}</main></body></html>"


def test_collect_failed_dedupes_and_drops_urls_that_succeeded_elsewhere() -> None:
    results = [{"business_id": 1, "pages": [
        {"url": "https://s.com/a", "status": "scrape_failed", "sources": ["map"], "kind_hint": "promotion", "error": "500"},
        {"url": "https://s.com/b", "status": "scrape_failed", "sources": ["map"], "kind_hint": "membership"}]},
               {"business_id": 1, "pages": [
        {"url": "https://s.com/a", "status": "scrape_failed", "sources": ["search"], "title": "A"},
        {"url": "https://s.com/b", "status": "valid", "sources": ["search"]}]}]
    got = collect_failed(results)
    assert list(got) == ["https://s.com/a"]  # /b succeeded for the same business
    assert got["https://s.com/a"]["sources"] == ["map", "search"]
    assert got["https://s.com/a"]["businesses"] == [1]


def test_a_chain_page_is_retried_once_and_the_result_replaces_every_branch_failure() -> None:
    page = {"url": "https://chain.com/specials", "status": "scrape_failed", "sources": ["map"],
            "kind_hint": "membership", "hop": 0, "via": None, "title": ""}
    results = [{"business_id": 1, "pages": [dict(page)]}, {"business_id": 2, "pages": [dict(page)]},
               {"business_id": 3, "pages": [{"url": "https://other.com/deals", "status": "rejected"}]}]
    asked = []

    def retry(url, record):
        asked.append((url, record["kind_hint"], record["businesses"]))
        return {"url": url, "status": "valid", "kind_hint": record["kind_hint"], "engine": "firecrawl", "retried": True}

    counts = retry_failed_pages(results, retry)
    assert asked == [("https://chain.com/specials", "membership", [1, 2])]
    assert [r["pages"][0]["status"] for r in results] == ["valid", "valid", "rejected"]
    assert counts == {"urls": 1, "valid": 1, "recovered_by_firecrawl": 1}


class _Fc:
    def __init__(self, outcomes):
        self.outcomes, self.calls = list(outcomes), 0

    def scrape(self, url):
        self.calls += 1
        outcome = self.outcomes.pop(0)
        if isinstance(outcome, Exception):
            raise outcome
        return outcome


RECORD = {"kind_hint": "promotion", "sources": ["search"], "title": "T", "hop": 0, "via": None}
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
    assert page["kind_hint"] == "promotion"  # 仍失败的记录保留类别，下次运行按原类别重试
