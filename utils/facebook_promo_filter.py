"""
Utilities for filtering Facebook posts with concrete promo / pricing signals.
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import date, datetime, timezone
from typing import Any, Dict, List, Optional
from urllib.parse import urlparse, urlunparse
from zoneinfo import ZoneInfo

from utils.caption_price_filter import PriceSignal, extract_price_signals


STRONG_PROMO_PATTERNS = [
    ("sale", re.compile(r"\b(?:sale|flash sale)\b", re.IGNORECASE)),
    ("special", re.compile(r"\b(?:special|specials)\b", re.IGNORECASE)),
    ("promo", re.compile(r"\b(?:promo|promotion|promotions)\b", re.IGNORECASE)),
    ("discount", re.compile(r"\b(?:discount|discounts|marked down)\b", re.IGNORECASE)),
    ("deal", re.compile(r"\b(?:deal|deals)\b", re.IGNORECASE)),
    ("offer", re.compile(r"\b(?:offer|offers)\b", re.IGNORECASE)),
    ("pricing", re.compile(r"\b(?:pricing|price drop|retails for)\b", re.IGNORECASE)),
    ("save", re.compile(r"\b(?:save|saving|stock up)\b", re.IGNORECASE)),
    ("off", re.compile(r"\b\d{1,3}%\s*off\b|\boff\b", re.IGNORECASE)),
    ("limited_time", re.compile(r"\b(?:limited time|while supplies last|for a limited time)\b", re.IGNORECASE)),
    ("bundle", re.compile(r"\b(?:bundle|bundles|package deal|buy one get one|bogo)\b", re.IGNORECASE)),
    ("exclusive", re.compile(r"\b(?:exclusive pricing|exclusive deal|member price|members only)\b", re.IGNORECASE)),
    ("retail", re.compile(r"\b(?:amazon|barnes and noble|shop now|order now|buy now|per customer)\b", re.IGNORECASE)),
]

WEAK_ONLY_PATTERNS = [
    ("countdown", re.compile(r"\b(?:\d+\s+(?:day|days|hour|hours|hr|hrs)\s+left|countdown|last chance|ends tonight)\b", re.IGNORECASE)),
    ("time_range", re.compile(r"\b\d{1,2}(?::\d{2})?\s*(?:am|pm)\s*(?:-|–|to)\s*\d{1,2}(?::\d{2})?\s*(?:am|pm)\b", re.IGNORECASE)),
    ("duration", re.compile(r"\b\d+(?:\.\d+)?\s*(?:hr|hrs|hour|hours|min|mins|minute|minutes|wk|wks|week|weeks|mo|month|months|yr|yrs|year|years)\b", re.IGNORECASE)),
]

LOCAL_TZ_NAME = "Asia/Shanghai"


@dataclass(frozen=True)
class PromoFilterResult:
    passed: bool
    price_signals: List[PriceSignal]
    promo_keyword_labels: List[str]
    weak_match_labels: List[str]


def normalize_facebook_profile_url(url: str) -> str:
    raw = (url or "").strip()
    if not raw:
        return ""
    candidate = raw if "://" in raw else f"https://{raw}"
    parsed = urlparse(candidate)
    host = (parsed.netloc or "").lower()
    if host.startswith("m."):
        host = "www." + host[2:]
    if not host:
        host = "www.facebook.com"
    if host == "facebook.com":
        host = "www.facebook.com"
    path = re.sub(r"/+", "/", parsed.path or "/").rstrip("/")
    if not path:
        return ""
    clean = parsed._replace(scheme="https", netloc=host, path=path.lower(), query="", fragment="")
    return urlunparse(clean)


def normalize_facebook_post_url(url: str) -> str:
    raw = (url or "").strip()
    if not raw:
        return ""
    candidate = raw if "://" in raw else f"https://{raw}"
    parsed = urlparse(candidate)
    host = (parsed.netloc or "").lower()
    if host.startswith("m."):
        host = "www." + host[2:]
    if host == "facebook.com":
        host = "www.facebook.com"
    path = re.sub(r"/+", "/", parsed.path or "/").rstrip("/")
    if not path:
        return ""
    clean = parsed._replace(scheme="https", netloc=host or "www.facebook.com", path=path, query="", fragment="")
    return urlunparse(clean)


def resolve_post_local_date(
    iso_timestamp: str,
    unix_timestamp: Any,
    *,
    timezone_name: str = LOCAL_TZ_NAME,
) -> Optional[date]:
    raw = (iso_timestamp or "").strip()
    if raw:
        normalized = raw.replace("Z", "+00:00")
        try:
            parsed = datetime.fromisoformat(normalized)
        except ValueError:
            parsed = None
        if parsed is not None:
            if parsed.tzinfo is None:
                parsed = parsed.replace(tzinfo=timezone.utc)
            return parsed.astimezone(ZoneInfo(timezone_name)).date()

    if unix_timestamp in (None, ""):
        return None
    try:
        epoch_value = int(unix_timestamp)
    except (TypeError, ValueError):
        return None
    parsed_epoch = datetime.fromtimestamp(epoch_value, tz=timezone.utc)
    return parsed_epoch.astimezone(ZoneInfo(timezone_name)).date()


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


def evaluate_facebook_promo_text(text: str) -> PromoFilterResult:
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

    strong_price_signals: List[PriceSignal] = []
    for signal in price_signals:
        signal_text = signal.match_text.lower()
        has_currency = bool(re.search(r"[$€£]|\b(?:usd|dollars?|bucks)\b", signal_text))
        if signal.label == "discount_percent":
            strong_price_signals.append(signal)
            continue
        if signal.label in {"currency_amount", "currency_word_amount", "save_amount", "per_unit_price", "membership_price"}:
            if has_currency and promo_keyword_labels:
                strong_price_signals.append(signal)
            continue
        if signal.label == "price_keyword" and has_currency:
            strong_price_signals.append(signal)

    has_strong_price = bool(strong_price_signals)
    has_keyword_offer = "discount" in promo_keyword_labels or "sale" in promo_keyword_labels or (
        len(promo_keyword_labels) >= 2 and "pricing" in promo_keyword_labels
    )
    weak_only = bool(weak_match_labels) and not has_strong_price and not has_keyword_offer

    return PromoFilterResult(
        passed=(has_strong_price or has_keyword_offer) and not weak_only,
        price_signals=strong_price_signals,
        promo_keyword_labels=promo_keyword_labels,
        weak_match_labels=weak_match_labels,
    )


def summarize_filtered_post(post: Dict[str, Any], *, timezone_name: str = LOCAL_TZ_NAME) -> Dict[str, Any]:
    text = (post.get("text") or "").strip()
    result = evaluate_facebook_promo_text(text)
    price_signal_labels = [signal.label for signal in result.price_signals]
    price_signal_matches = [signal.match_text for signal in result.price_signals]
    normalized_post_url = normalize_facebook_post_url(post.get("url") or "")
    normalized_input_url = normalize_facebook_profile_url(post.get("inputUrl") or "")
    local_post_date = resolve_post_local_date(post.get("time") or "", post.get("timestamp"), timezone_name=timezone_name)

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
