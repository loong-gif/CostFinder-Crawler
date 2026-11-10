"""Value review of discovered promotion / membership pages and the add-to-monitor decision.

The review never writes anything: it scores a page that already passed the price-evidence gate and
returns ``add``, ``add_with_review`` or ``skip`` with reasons. Adding a URL to the monitor only starts
baseline capture; the monitor's own visibility and review gates still decide what reaches the database.
"""
from __future__ import annotations

import datetime as dt
from dataclasses import dataclass
from typing import Any, Dict, List, Mapping, Optional
from urllib.parse import urlparse

from config.discovery_filters import passes_filter
import re

from utils.promo_page_discovery import BLOG_PATH_RE, SERVICE_RE, is_aggregator

# Educational "how much does X cost" articles quote market-wide ranges, not the merchant's own price or offer.
INFORMATIONAL_PATH_RE = re.compile(r"how-much|how-?much|what-is|cost-of|average-cost|price-guide|guide-to|faq|"
                                   r"vs-|-vs|pros-and-cons|worth-it|before-and-after|recovery|aftercare", re.I)
MARKET_RANGE_RE = re.compile(r"\b(?:on average|averages?|typically|usually|generally|commonly|can vary|varies|"
                             r"ranges?\s+(?:from|between)|between\s+\$\d+\s+and)\b", re.I)

ADD_THRESHOLD = 65
REVIEW_THRESHOLD = 45
MAX_PAGES_PER_BUSINESS = 6


@dataclass(frozen=True)
class BusinessContext:
    business_id: int
    name: str
    city: str
    review_count: Optional[int]
    score: Optional[float]
    website: str
    has_active_offer: bool  # active promotion or any membership already stored
    never_processed: bool   # nothing stored at all: promotions, services, offers, memberships


def review_page(page: Mapping[str, Any], business: BusinessContext, today: Optional[dt.date] = None) -> Dict[str, Any]:
    today = today or dt.date.today()
    url = str(page.get("url") or "")
    parsed = urlparse(url)
    host = (parsed.hostname or "").lower()
    reasons: List[str] = []
    status = page.get("status")

    def skip(reason: str, score: int = 0) -> Dict[str, Any]:
        return {"decision": "skip", "score": score, "reasons": [reason]}

    if status not in ("valid", "needs_review"):
        return skip(f"status_{status}")
    if is_aggregator(host.removeprefix("www.")):
        return skip("aggregator_or_directory")
    if (parsed.path or "/") == "/":
        return skip("homepage")
    evidence = [str(e) for e in page.get("evidence") or []]
    if not evidence:
        return skip("no_evidence")
    end_dates = [d for d in page.get("end_dates") or [] if d]
    if end_dates and max(end_dates) < today.isoformat():
        return skip(f"expired_{max(end_dates)}")

    path = parsed.path or "/"
    market_range = sum(1 for e in evidence if MARKET_RANGE_RE.search(e))
    if INFORMATIONAL_PATH_RE.search(path) and market_range >= max(1, len(evidence) // 2) \
            and not page.get("kind") == "membership":
        return skip("informational_article")

    score = 40 if status == "valid" else 25
    reasons.append(f"status_{status}")
    lines = min(len(evidence), 3)
    score += 8 * lines
    reasons.append(f"{len(evidence)}_evidence_lines")
    if any("$" in e for e in evidence):
        score += 8
        reasons.append("states_amounts")
    if any(SERVICE_RE.search(e) for e in evidence):
        score += 8
        reasons.append("names_a_service")
    kind = page.get("kind") or page.get("kind_hint") or ""
    if kind in ("promotion", "membership"):
        score += 8
    elif kind == "pricing":
        score += 4
    sources = page.get("sources") or []
    if len(set(sources)) >= 2:
        score += 5
        reasons.append("found_by_" + "+".join(sorted(set(sources))))
    if end_dates:
        soonest = min(d for d in end_dates if d >= today.isoformat())
        if (dt.date.fromisoformat(soonest) - today).days <= 90:
            score += 4
            reasons.append(f"ends_{soonest}")
    if passes_filter({"review_count": business.review_count, "score": business.score, "website": business.website}):
        score += 8
    else:
        score -= 10
        reasons.append("business_below_filter_line")
    if business.never_processed:
        score += 12
        reasons.append("business_never_processed")
    elif not business.has_active_offer:
        score += 8
        reasons.append("business_has_no_active_offer")
    if BLOG_PATH_RE.search(parsed.path or ""):
        score -= 15
        reasons.append("blog_post")
    score = max(0, min(100, score))

    if status == "valid" and score >= ADD_THRESHOLD:
        decision = "add"
    elif score >= REVIEW_THRESHOLD:
        decision = "add_with_review"
        if status == "needs_review":
            reasons.append("visibility_needs_review")
    else:
        decision = "skip"
        reasons.append("low_value")
    return {"decision": decision, "score": score, "reasons": reasons}


def decide(pages: List[Dict[str, Any]], businesses: Mapping[int, BusinessContext],
           today: Optional[dt.date] = None) -> List[Dict[str, Any]]:
    """Review every page, drop duplicates by (URL, business) and cap how many pages one business adds.

    连锁品牌的多家分店共用同一个页面时，每家分店各保留一条决策。
    """
    out: Dict[tuple, Dict[str, Any]] = {}
    for page in pages:
        biz = businesses.get(int(page["business_id"]))
        if biz is None:
            continue
        verdict = review_page(page, biz, today)
        record = {
            "url": page["url"], "business_id": biz.business_id, "business": biz.name, "city": biz.city,
            "kind": page.get("kind") or page.get("kind_hint"), "status": page.get("status"),
            "sources": page.get("sources") or [], "title": page.get("title") or "",
            "end_dates": page.get("end_dates") or [], "evidence": (page.get("evidence") or [])[:3], **verdict,
        }
        key = (page["url"], biz.business_id)
        previous = out.get(key)
        # 重试抓到的结果（哪怕被排除）取代同分的抓取失败记录，跳过原因才如实
        if (previous is None or record["score"] > previous["score"]
                or (record["score"] == previous["score"] and previous["status"] == "scrape_failed"
                    and record["status"] != "scrape_failed")):
            out[key] = record
    records = sorted(out.values(), key=lambda r: (-r["score"], r["url"]))
    per_business: Dict[int, int] = {}
    seen_evidence: Dict[int, set] = {}
    for record in records:
        if record["decision"] == "skip":
            continue
        # A page whose evidence is already carried by a better page of the same business adds nothing
        # (boilerplate such as "$50 off your first visit" repeated across SEO landing pages).
        lines = {" ".join(e.casefold().split()) for e in record["evidence"]}
        covered = seen_evidence.setdefault(record["business_id"], set())
        if lines and lines <= covered:
            record["decision"] = "skip"
            record["reasons"].append("duplicate_evidence")
            continue
        covered |= lines
        count = per_business.get(record["business_id"], 0)
        if count >= MAX_PAGES_PER_BUSINESS:
            record["decision"] = "skip"
            record["reasons"].append("business_page_cap")
        else:
            per_business[record["business_id"]] = count + 1
    return records
