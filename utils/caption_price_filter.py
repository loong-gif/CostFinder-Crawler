"""
Utilities for detecting price-related signals in social captions.
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from typing import List


@dataclass(frozen=True)
class PriceSignal:
    label: str
    match_text: str


PRICE_SIGNAL_PATTERNS = [
    (
        "currency_amount",
        re.compile(
            r"(?<!\w)(?:[$€£]\s?\d{1,4}(?:,\d{3})*(?:\.\d{1,2})?(?:\s*[-/]\s*[$€£]?\d{1,4}(?:,\d{3})*(?:\.\d{1,2})?)?)",
            re.IGNORECASE,
        ),
    ),
    (
        "currency_word_amount",
        re.compile(
            r"(?<!\w)\d{1,4}(?:,\d{3})*(?:\.\d{1,2})?\s*(?:usd|dollars?|bucks)\b",
            re.IGNORECASE,
        ),
    ),
    (
        "discount_percent",
        re.compile(
            r"\b(?:save|get|take|enjoy|extra)?\s*\d{1,3}%\s*(?:off|discount|savings?)\b",
            re.IGNORECASE,
        ),
    ),
    (
        "save_amount",
        re.compile(
            r"\b(?:save|saving|savings of|get)\s+(?:up to\s+)?[$€£]?\s?\d{1,4}(?:,\d{3})*(?:\.\d{1,2})?\b",
            re.IGNORECASE,
        ),
    ),
    (
        "price_keyword",
        re.compile(
            r"\b(?:from|starting at|as low as|now|only|just)\s+[$€£]?\s?\d{1,4}(?:,\d{3})*(?:\.\d{1,2})?\b",
            re.IGNORECASE,
        ),
    ),
    (
        "per_unit_price",
        re.compile(
            r"(?:(?:[$€£]\s?\d{1,4}(?:,\d{3})*(?:\.\d{1,2})?)|(?:\d{1,4}(?:,\d{3})*(?:\.\d{1,2})?\s*(?:usd|dollars?)))\s*(?:/|per)\s*(?:unit|u|syringe|half syringe|session|treatment|area|ml|month|weekly|week|year|package|laser session|vial)\b",
            re.IGNORECASE,
        ),
    ),
    (
        "membership_price",
        re.compile(
            r"\b(?:membership|member price|members only|join for)\b[^.\n]{0,40}[$€£]?\s?\d{1,4}(?:,\d{3})*(?:\.\d{1,2})?",
            re.IGNORECASE,
        ),
    ),
]

TEMPORAL_CONTINUATION_PATTERN = re.compile(
    r"^\s*(?:"
    r"(?:-|–|to)\s*\d{1,2}(?::\d{2})?\s*(?:am|pm)\b|"
    r"day|days|hour|hours|hr|hrs|minute|minutes|min|mins|"
    r"week|weeks|wk|wks|month|months|year|years|yr|yrs|"
    r"am|pm|session|sessions|left|remaining|countdown|spots?"
    r")\b",
    re.IGNORECASE,
)


def normalize_caption_text(text: str) -> str:
    if not text:
        return ""
    return re.sub(r"\s+", " ", text).strip()


def extract_price_signals(text: str) -> List[PriceSignal]:
    normalized = normalize_caption_text(text)
    if not normalized:
        return []

    signals: List[PriceSignal] = []
    seen = set()

    for label, pattern in PRICE_SIGNAL_PATTERNS:
        for match in pattern.finditer(normalized):
            snippet = match.group(0).strip(" ,.;:()[]{}")
            if not snippet:
                continue
            if label == "price_keyword":
                trailing_text = normalized[match.end() : match.end() + 24]
                if TEMPORAL_CONTINUATION_PATTERN.match(trailing_text):
                    continue
            dedupe_key = (label, snippet.lower())
            if dedupe_key in seen:
                continue
            seen.add(dedupe_key)
            signals.append(PriceSignal(label=label, match_text=snippet))

    return signals


def caption_contains_price_info(text: str) -> bool:
    return bool(extract_price_signals(text))
