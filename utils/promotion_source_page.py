"""Classify scraped pages before clinic_promotions extract/write."""
from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Mapping
from urllib.parse import urlparse

from utils.clinic_services_search import is_article_service_url
from utils.recent_raw_extraction import GateDecision, SIGNAL_LINE

PAGE_TYPE_SPECIAL = "special"
PAGE_TYPE_MEMBERSHIP = "membership"
PAGE_TYPE_PRICING = "pricing"
PAGE_TYPE_BLOG = "blog"
PAGE_TYPE_GENERIC = "generic"

PROMOTION_WRITE_PAGE_TYPES = frozenset({PAGE_TYPE_SPECIAL})

_MEMBERSHIP_PATH_RE = re.compile(
    r"/(?:memberships?|member(?:-)?club|members?-?club|vip(?:-|\s)?beauty|beauty-bank|"
    r"rewards?-?program|loyalty)(?:/|$)",
    re.IGNORECASE,
)
_PRICING_PATH_RE = re.compile(
    r"/(?:services?|pricing|price-list|menu|treatments?|injectables?)(?:/|$)",
    re.IGNORECASE,
)
_PROMO_PATH_RE = re.compile(
    r"/(?:specials?|promos?|promotions?|offers?|deals?)(?:/|$)|"
    r"(?:^|/)(?:summer|winter|spring|fall|january|february|march|april|may|june|july|"
    r"august|september|october|november|december)[-_ ]specials?",
    re.IGNORECASE,
)
_BLOG_PATH_RE = re.compile(
    r"/(?:blog|news|articles?|posts?|stories?)(?:/|$)|"
    r"(?:vaccine|covid|before-and-after|what-is-|how-to-|guide-to-)",
    re.IGNORECASE,
)
_BLOG_CONTENT_RE = re.compile(
    r"\b(?:vaccine|covid-?19|blog post|min(?:ute)?s?\s+read|posted on|table of contents|"
    r"share this article|related articles)\b",
    re.IGNORECASE,
)
_MEMBERSHIP_CONTENT_RE = re.compile(
    r"\b(?:membership(?:s)?|member(?:s)?\s+only|monthly\s+dues|per\s+month|/\s*mo\b|"
    r"join(?:\s+our)?\s+membership|members?\s+club|beauty\s+bank|vitality\s+plan|"
    r"annual\s+commitment|tier\s+\d|membership\s+tier)\b",
    re.IGNORECASE,
)
_GENERIC_UI_RE = re.compile(
    r"\b(?:contact\s+us|subtotal\s*\$?\s*0\.?0{2}|your\s+cart\s+is\s+empty|"
    r"checkout\s+-\s*empty|book\s+an?\s+appointment\s+today\s+only)\b",
    re.IGNORECASE,
)
_JSON_TITLE_RE = re.compile(r"^\s*[\{\[]")
_PROMO_CONTENT_RE = re.compile(
    r"\b(?:special(?:s)?|promo(?:tion)?s?|limited\s+time|save\s+\$|save\s+\d+%|"
    r"new\s+patient\s+offer|\d+\s*%\s*off)\b",
    re.IGNORECASE,
)


@dataclass(frozen=True)
class SourcePageClassification:
    page_type: str
    reason: str


def _blob(*parts: str) -> str:
    return "\n".join(str(part or "").strip() for part in parts if str(part or "").strip())


def _path(url: str) -> str:
    return urlparse(str(url or "").strip()).path or ""


def classify_promotion_source_page(
    *,
    url: str,
    title: str = "",
    markdown: str = "",
    metadata: Mapping[str, str] | None = None,
) -> SourcePageClassification:
    """Return page channel type for promotion scrape evidence."""
    meta = metadata or {}
    page_title = str(title or meta.get("title") or "").strip()
    body = _blob(page_title, markdown)
    path = _path(url)
    path_cf = path.casefold()

    if _JSON_TITLE_RE.match(page_title):
        return SourcePageClassification(PAGE_TYPE_GENERIC, "promotion_title_looks_like_json")

    if _MEMBERSHIP_PATH_RE.search(path) or path_cf.rstrip("/").endswith("/memberships"):
        return SourcePageClassification(PAGE_TYPE_MEMBERSHIP, "membership_url_segment")

    if _MEMBERSHIP_CONTENT_RE.search(body) and not (
        _PROMO_PATH_RE.search(path) and _PROMO_CONTENT_RE.search(body)
    ):
        return SourcePageClassification(PAGE_TYPE_MEMBERSHIP, "membership_page")

    if is_article_service_url(url) or _BLOG_PATH_RE.search(path) or _BLOG_CONTENT_RE.search(body):
        if not (_PROMO_PATH_RE.search(path) and _PROMO_CONTENT_RE.search(body) and SIGNAL_LINE.search(body)):
            return SourcePageClassification(PAGE_TYPE_BLOG, "blog_or_article_page")

    if _GENERIC_UI_RE.search(body) and not SIGNAL_LINE.search(body):
        return SourcePageClassification(PAGE_TYPE_GENERIC, "generic_contact_or_cart_ui")

    if _PRICING_PATH_RE.search(path) and not _PROMO_PATH_RE.search(path):
        if not _PROMO_CONTENT_RE.search(body):
            return SourcePageClassification(PAGE_TYPE_PRICING, "service_price_list_page")

    if _PROMO_PATH_RE.search(path) or _PROMO_CONTENT_RE.search(body):
        if SIGNAL_LINE.search(body) or _PROMO_CONTENT_RE.search(body):
            return SourcePageClassification(PAGE_TYPE_SPECIAL, "promotion_or_specials_page")

    if _PRICING_PATH_RE.search(path):
        return SourcePageClassification(PAGE_TYPE_PRICING, "service_price_list_page")

    return SourcePageClassification(PAGE_TYPE_GENERIC, "unclassified_non_promo_page")


def gate_promotion_source_page(
    *,
    url: str,
    title: str = "",
    markdown: str = "",
    metadata: Mapping[str, str] | None = None,
) -> GateDecision:
    classification = classify_promotion_source_page(
        url=url,
        title=title,
        markdown=markdown,
        metadata=metadata,
    )
    if classification.page_type not in PROMOTION_WRITE_PAGE_TYPES:
        return GateDecision(
            False,
            None,
            f"ineligible_page_type:{classification.page_type}:{classification.reason}",
        )
    return GateDecision(True, None, classification.reason)


def _self_check() -> None:
    blog = classify_promotion_source_page(
        url="https://clinic.example/blog/covid-vaccine-update",
        title="COVID Vaccine Information",
        markdown="Posted on March 1. Related articles about vaccine safety.",
    )
    assert blog.page_type == PAGE_TYPE_BLOG

    membership = classify_promotion_source_page(
        url="https://clinic.example/membership",
        title="Vitality Membership",
        markdown="Join our membership. Vitality plan $149/month. Member benefits include discounts.",
    )
    assert membership.page_type == PAGE_TYPE_MEMBERSHIP

    special = classify_promotion_source_page(
        url="https://clinic.example/specials",
        title="July Specials",
        markdown="Botox $11/unit. Save 20% on filler this month.",
    )
    assert special.page_type == PAGE_TYPE_SPECIAL

    generic = classify_promotion_source_page(
        url="https://clinic.example/occasion",
        title='{"title":"Intro"}',
        markdown="Contact us. Subtotal $0.00",
    )
    assert generic.page_type == PAGE_TYPE_GENERIC


if __name__ == "__main__":
    _self_check()
    print("promotion_source_page self-check ok")
