"""
Utilities for filtering Instagram posts with concrete promo / pricing signals.
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import date, datetime
from typing import Any, Dict, List, Optional
from urllib.parse import urlparse, urlunparse
from zoneinfo import ZoneInfo

from utils.caption_price_filter import PriceSignal, extract_price_signals


STRONG_PROMO_PATTERNS = [
    ("sale", re.compile(r"\b(?:sale|flash sale)\b", re.IGNORECASE)),
    ("special", re.compile(r"\b(?:special|specials)\b", re.IGNORECASE)),
    ("promo", re.compile(r"\b(?:promo|promotion|promotions)\b", re.IGNORECASE)),
    ("discount", re.compile(r"\b(?:discount|discounts)\b", re.IGNORECASE)),
    ("deal", re.compile(r"\b(?:deal|deals)\b", re.IGNORECASE)),
    ("offer", re.compile(r"\b(?:offer|offers)\b", re.IGNORECASE)),
    ("pricing", re.compile(r"\b(?:pricing|priced)\b", re.IGNORECASE)),
    ("save", re.compile(r"\b(?:save|saving|savings)\b", re.IGNORECASE)),
    ("off", re.compile(r"\b\d{1,3}%\s*off\b|\boff\b", re.IGNORECASE)),
    ("complimentary", re.compile(r"\bcomplimentary\b", re.IGNORECASE)),
    ("bundle", re.compile(r"\b(?:bundle|bundles|package deal|buy one get one|bogo)\b", re.IGNORECASE)),
    ("exclusive", re.compile(r"\b(?:exclusive pricing|exclusive deal|member price|members only)\b", re.IGNORECASE)),
]

WEAK_ONLY_PATTERNS = [
    ("countdown", re.compile(r"\b(?:\d+\s+(?:day|days|hour|hours|hr|hrs)\s+left|countdown|last chance|ends tonight)\b", re.IGNORECASE)),
    ("today_only", re.compile(r"\b(?:today only|tomorrow only|this weekend only|tonight only)\b", re.IGNORECASE)),
    ("time_range", re.compile(r"\b\d{1,2}(?::\d{2})?\s*(?:am|pm)\s*(?:-|–|to)\s*\d{1,2}(?::\d{2})?\s*(?:am|pm)\b", re.IGNORECASE)),
    ("duration", re.compile(r"\b\d+(?:\.\d+)?\s*(?:hr|hrs|hour|hours|min|mins|minute|minutes|wk|wks|week|weeks|mo|month|months|yr|yrs|year|years)\b", re.IGNORECASE)),
    ("range_duration", re.compile(r"\b\d+\s*(?:-|–|to)\s*\d+\s*(?:day|days|week|weeks|month|months|year|years|hr|hrs|hour|hours)\b", re.IGNORECASE)),
    ("session_counter", re.compile(r"\b(?:session\s*\d+\s*of\s*\d+|\d+\s*of\s*\d+)\b", re.IGNORECASE)),
]

LOCAL_TZ_NAME = "Asia/Shanghai"


@dataclass(frozen=True)
class PromoFilterResult:
    passed: bool
    price_signals: List[PriceSignal]
    promo_keyword_labels: List[str]
    weak_match_labels: List[str]


def normalize_instagram_profile_url(url: str) -> str:
    raw = (url or "").strip()
    if not raw:
        return ""
    candidate = raw if "://" in raw else f"https://{raw}"
    parsed = urlparse(candidate)
    host = (parsed.netloc or parsed.path.split("/")[0]).lower()
    path = parsed.path if parsed.netloc else "/" + "/".join(parsed.path.split("/")[1:])
    segments = [segment for segment in path.split("/") if segment]
    if not segments:
        return ""
    username = segments[0].lstrip("@").lower()
    clean = parsed._replace(scheme="https", netloc="www.instagram.com", path=f"/{username}", query="", fragment="")
    return urlunparse(clean)


def normalize_instagram_post_url(url: str) -> str:
    raw = (url or "").strip()
    if not raw:
        return ""
    candidate = raw if "://" in raw else f"https://{raw}"
    parsed = urlparse(candidate)
    segments = [segment for segment in parsed.path.split("/") if segment]
    if len(segments) >= 2 and segments[0] in {"p", "reel", "tv"}:
        clean_path = f"/{segments[0]}/{segments[1]}"
    else:
        clean_path = parsed.path.rstrip("/") or "/"
    clean = parsed._replace(scheme="https", netloc="www.instagram.com", path=clean_path, query="", fragment="")
    return urlunparse(clean)


def resolve_post_local_date(timestamp_value: str, timezone_name: str = LOCAL_TZ_NAME) -> Optional[date]:
    raw = (timestamp_value or "").strip()
    if not raw:
        return None
    normalized = raw.replace("Z", "+00:00")
    try:
        parsed = datetime.fromisoformat(normalized)
    except ValueError:
        return None
    if parsed.tzinfo is None:
        return None
    return parsed.astimezone(ZoneInfo(timezone_name)).date()


def extract_promo_keyword_labels(text: str) -> List[str]:
    labels: List[str] = []
    seen = set()
    for label, pattern in STRONG_PROMO_PATTERNS:
        if label in seen:
            continue
        if pattern.search(text):
            labels.append(label)
            seen.add(label)
    return labels


def extract_weak_match_labels(text: str) -> List[str]:
    labels: List[str] = []
    seen = set()
    for label, pattern in WEAK_ONLY_PATTERNS:
        if label in seen:
            continue
        if pattern.search(text):
            labels.append(label)
            seen.add(label)
    return labels


def evaluate_instagram_promo_caption(text: str) -> PromoFilterResult:
    normalized = re.sub(r"\s+", " ", text or "").strip()
    if not normalized:
        return PromoFilterResult(
            passed=False,
            price_signals=[],
            promo_keyword_labels=[],
            weak_match_labels=[],
        )

    price_signals = extract_price_signals(normalized)
    promo_keyword_labels = extract_promo_keyword_labels(normalized)
    weak_match_labels = extract_weak_match_labels(normalized)

    has_strong_price = bool(price_signals)
    has_keyword_offer = len(promo_keyword_labels) >= 2 or any(
        label in {"sale", "special", "promo", "discount", "deal", "offer", "pricing"}
        for label in promo_keyword_labels
    )
    weak_only = bool(weak_match_labels) and not has_strong_price and not has_keyword_offer

    return PromoFilterResult(
        passed=(has_strong_price or has_keyword_offer) and not weak_only,
        price_signals=price_signals,
        promo_keyword_labels=promo_keyword_labels,
        weak_match_labels=weak_match_labels,
    )


def summarize_filtered_post(post: Dict[str, Any], *, timezone_name: str = LOCAL_TZ_NAME) -> Dict[str, Any]:
    caption = (post.get("caption") or "").strip()
    result = evaluate_instagram_promo_caption(caption)
    price_signal_labels = [signal.label for signal in result.price_signals]
    price_signal_matches = [signal.match_text for signal in result.price_signals]
    normalized_post_url = normalize_instagram_post_url(post.get("url") or "")
    normalized_input_url = normalize_instagram_profile_url(post.get("inputUrl") or "")
    local_post_date = resolve_post_local_date(post.get("timestamp") or "", timezone_name=timezone_name)

    return {
        **post,
        "url": normalized_post_url or (post.get("url") or ""),
        "inputUrl": normalized_input_url or (post.get("inputUrl") or ""),
        "local_post_date": local_post_date.isoformat() if local_post_date else "",
        "matched_price_signals": price_signal_matches,
        "matched_price_signal_labels": price_signal_labels,
        "matched_price_signal_count": len(price_signal_matches),
        "matched_promo_keyword_labels": result.promo_keyword_labels,
        "matched_weak_labels": result.weak_match_labels,
        "passed_promo_filter": result.passed,
    }
