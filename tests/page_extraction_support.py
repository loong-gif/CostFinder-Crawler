from __future__ import annotations

import hashlib
from datetime import datetime, timezone

from utils.page_extraction import build_plan
from utils.page_extraction_snapshot import latest_scrapes
from utils.page_extraction_source_review import SOURCE_REVIEW_SCHEMA

BUSINESS = 1
NAMES = {BUSINESS: "Glow Clinic"}
UPDATED_AT = datetime(2026, 10, 1, tzinfo=timezone.utc)
EMPTY_STATE = {"names": NAMES, "promotions": {}, "memberships": {}}


def promo_item(title: str, *lines: str, end: str | None = None) -> dict:
    return {"promotion_title": title, "promotion_content": list(lines), "campaign_start_date": None,
            "campaign_end_date": end}


class FakeLLM:
    """按页面 URL 返回预设结果：来源复核请求返回 reviews，抽取请求返回 payloads；记录调用次数。"""

    model = "fake-model"

    def __init__(self, payloads: dict[str, dict], reviews: dict[str, dict]) -> None:
        self.payloads = payloads
        self.reviews = reviews
        self.calls = 0
        self.prompts: list[str] = []

    def create_json_response(self, messages, json_schema=None, **kwargs):
        self.calls += 1
        user = messages[-1]["content"]
        self.prompts.append(user)
        answers = self.reviews if json_schema is SOURCE_REVIEW_SCHEMA else self.payloads
        for url, payload in answers.items():
            if f"Source URL: {url}\n" in user:
                return payload
        raise AssertionError(f"unexpected request: {user[:200]}")


def promo_review(*cards: tuple[str, list[str]], page_type: str = "promotion") -> dict:
    """来源复核：每张卡片为 (标题, 其余原文行)。"""
    return {"page_type": page_type, "memberships": [],
            "promotion_quotes": [{"title": title, "lines": lines} for title, lines in cards]}


def membership_review(*plans: tuple[str, list[str]]) -> dict:
    return {"page_type": "membership", "promotion_quotes": [],
            "memberships": [{"name": name, "source_claims": claims} for name, claims in plans]}


def promo_llm(pages: dict[str, tuple[list[dict], dict]]) -> FakeLLM:
    """pages: URL → (抽取出的优惠, 来源复核)。"""
    return FakeLLM({url: {"explanation": "x", "promotions": items} for url, (items, _) in pages.items()},
                   {url: review for url, (_, review) in pages.items()})


def scrape(url: str, markdown: str, title: str | None, *, fingerprint: str | None = None) -> dict:
    return {"id": 1, "request_fingerprint": fingerprint or hashlib.sha256(url.encode()).hexdigest(),
            "source_url": url, "success": True,
            "markdown": markdown, "updated_at": "2026-10-29T00:00:00+00:00",
            "metadata": {"title": title} if title is not None else {}}


def plan_for(pages, scrapes, llm, tmp_path, *, fetch_title=None, state=EMPTY_STATE, manifest=None):
    def no_fetch(url, name):
        raise AssertionError(f"unexpected title fetch: {url}")

    return build_plan(pages, scrapes=latest_scrapes(scrapes), state=state, llm=llm,
                      fetch_title=fetch_title or no_fetch, cache_dir=tmp_path / "cache", manifest=manifest,
                      promotion_schema={}, membership_schema={})


def page(url: str, kind: str = "promotion") -> dict:
    return {"url": url, "business_id": BUSINESS, "kind": kind}


def entry_for(plan: dict, **match) -> dict:
    found = [e for e in plan["entries"] if all(e.get(k) == v for k, v in match.items())]
    assert len(found) == 1, plan["entries"]
    return found[0]


BOTOX = "Botox Bonus\nSave 20% on Botox."
BOTOX_PAGE = ([promo_item("Botox Bonus", "Save 20% on Botox.")], promo_review(("Botox Bonus", ["Save 20% on Botox."])))


def visible(*cards: str) -> dict:
    return {"status": "page_accessible",
            "claimResults": [{"recordType": "promotion", "claim": card, "status": "visible_match"} for card in cards]}


def membership_item(name: str, price: float) -> dict:
    return {"membership_name": name, "membership_price": price, "billing_period": "monthly",
            "minimum_commitment_months": None, "membership_description": None, "benefits": None}


def membership_llm(url: str, items: list[dict], review: dict) -> FakeLLM:
    return FakeLLM({url: {"explanation": "x", "memberships": items}}, {url: review})


HUB = "https://glow.example/specials"
HUB_KEY = (BUSINESS, "glow.example/specials")


def hub_state(*content: str) -> dict:
    old = {"promotion_id": 9, "promotion_title": "Specials | Glow Clinic", "promotion_content": list(content),
           "campaign_start_date": None, "campaign_end_date": None, "is_active": True, "updated_at": UPDATED_AT}
    return {**EMPTY_STATE, "promotions": {HUB_KEY: old}}
