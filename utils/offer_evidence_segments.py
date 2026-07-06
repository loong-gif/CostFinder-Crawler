"""Segment/evidence helpers for promo_website_staging.page_content.

The functions here are intentionally pure: they do not call Supabase and they do
not mutate existing rows. They turn the current ``[SEGMENT n]`` page_content
format into rows that match ``config/sql/offer_evidence_pipeline.sql``.
"""
from __future__ import annotations

import hashlib
import re
from dataclasses import asdict, dataclass
from decimal import Decimal, InvalidOperation
from typing import Any, Iterable, List
from urllib.parse import parse_qsl, urlencode, urlparse, urlunparse

_SEGMENT_PATTERN = re.compile(r"\[SEGMENT\s+(\d+)\]", re.IGNORECASE)
_WHITESPACE_PATTERN = re.compile(r"\s+")
_PRICE_PATTERN = re.compile(r"\$\s*(\d+(?:,\d{3})*(?:\.\d{1,2})?)", re.IGNORECASE)
_PERCENT_PATTERN = re.compile(r"\b\d+(?:\.\d+)?\s*%\s*(?:off|discount|savings?)\b", re.IGNORECASE)

TRACKING_QUERY_KEYS = {
    "fbclid",
    "gclid",
    "mc_cid",
    "mc_eid",
    "utm_campaign",
    "utm_content",
    "utm_medium",
    "utm_source",
    "utm_term",
}

SERVICE_HINTS = {
    "botox": "Botox",
    "dysport": "Dysport",
    "daxxify": "Daxxify",
    "letybo": "Letybo",
    "neurotoxin": "Neurotoxin",
    "tox": "Neurotoxin",
    "filler": "Dermal Filler",
    "fillers": "Dermal Filler",
    "juvederm": "Juvederm",
    "juvéderm": "Juvederm",
    "restylane": "Restylane",
    "radiesse": "Radiesse",
    "sculptra": "Sculptra",
    "kybella": "Kybella",
    "morpheus": "Morpheus8",
    "morpheus8": "Morpheus8",
    "microneedling": "Microneedling",
    "skinpen": "SkinPen",
    "hydrafacial": "HydraFacial",
    "facial": "Facial",
    "laser": "Laser",
    "ultherapy": "Ultherapy",
    "coolsculpting": "CoolSculpting",
    "diamondglow": "DiamondGlow",
    "latisse": "Latisse",
    "lip flip": "Lip Flip",
    "prf": "PRF",
}

BRAND_HINTS = {
    "allē": "Allē",
    "alle": "Allē",
    "aspire": "Aspire",
    "galderma": "Galderma",
    "allergan": "Allergan",
    "brilliant distinctions": "Brilliant Distinctions",
}

OFFER_TERM_HINTS = {
    "first time": "first_time_patient",
    "first-time": "first_time_patient",
    "new patient": "first_time_patient",
    "member": "membership",
    "membership": "membership",
    "monthly": "monthly",
    "package": "package",
    "buy ": "buy_x_get_y",
    "get ": "buy_x_get_y",
    "per unit": "per_unit",
    "/unit": "per_unit",
    "syringe": "per_syringe",
    "vial": "per_vial",
    "gift card": "gift_card",
    "rebate": "rebate",
    "coupon": "coupon",
    "limited time": "limited_time",
    "special": "special",
    "promo": "promo",
}

BOILERPLATE_TERMS = {
    "book now",
    "learn more",
    "claim now",
    "call now",
    "our locations",
    "contact us",
    "menu",
}


@dataclass(frozen=True)
class ParsedSegment:
    segment_index: int
    text: str
    text_normalized: str
    text_hash: str
    semantic_hash: str
    segment_identity_hash: str
    segment_type: str
    heading_context: str
    price_values: List[float]
    service_mentions: List[str]
    brand_mentions: List[str]
    offer_terms: List[str]
    is_price_signal: bool
    is_offer_signal: bool
    content_quality_score: float

    def to_record(self, row: dict[str, Any]) -> dict[str, Any]:
        source_url = str(row.get("subpage_url") or "")
        return {
            "promo_website_id": row.get("promo_website_id"),
            "business_id": row.get("business_id"),
            "source_url": source_url,
            "source_url_normalized": normalize_url(source_url),
            **asdict(self),
        }


def sha256_text(value: str) -> str:
    if not value:
        return ""
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def normalize_url(url: Any) -> str:
    raw = str(url or "").strip()
    if not raw:
        return ""
    candidate = raw if "://" in raw else f"https://{raw}"
    parsed = urlparse(candidate)
    host = (parsed.netloc or parsed.path.split("/")[0]).lower()
    if host.startswith("www."):
        host = host[4:]
    query_pairs = [
        (key, value)
        for key, value in parse_qsl(parsed.query, keep_blank_values=True)
        if key.lower() not in TRACKING_QUERY_KEYS
    ]
    path = parsed.path or "/"
    if path != "/":
        path = path.rstrip("/") or "/"
    return urlunparse(("https", host, path, "", urlencode(query_pairs), ""))


def normalize_segment_text(value: Any) -> str:
    text = str(value or "")
    text = text.replace("\u00a0", " ")
    text = text.replace("—", "-").replace("–", "-")
    text = re.sub(r"\$\s+", "$", text)
    text = re.sub(r"\bper\s+unit\b", "per unit", text, flags=re.IGNORECASE)
    text = _WHITESPACE_PATTERN.sub(" ", text).strip().lower()
    return text


def split_page_content(page_content: Any) -> List[tuple[int, str]]:
    text = str(page_content or "")
    matches = list(_SEGMENT_PATTERN.finditer(text))
    if not matches:
        stripped = text.strip()
        return [(0, stripped)] if stripped else []

    segments: List[tuple[int, str]] = []
    for idx, match in enumerate(matches):
        start = match.end()
        end = matches[idx + 1].start() if idx + 1 < len(matches) else len(text)
        body = text[start:end].strip()
        if body:
            segments.append((int(match.group(1)), body))
    return segments


def extract_price_values(text: str) -> List[float]:
    values: List[float] = []
    for match in _PRICE_PATTERN.finditer(text or ""):
        raw = match.group(1).replace(",", "")
        try:
            value = float(Decimal(raw))
        except (InvalidOperation, ValueError):
            continue
        values.append(value)
    return values


def _dedupe(values: Iterable[str]) -> List[str]:
    seen: set[str] = set()
    out: List[str] = []
    for value in values:
        key = value.lower()
        if key in seen:
            continue
        seen.add(key)
        out.append(value)
    return out


def extract_mentions(text_normalized: str, hints: dict[str, str]) -> List[str]:
    mentions: List[str] = []
    padded = f" {text_normalized} "
    for needle, label in hints.items():
        if " " in needle:
            if needle in text_normalized:
                mentions.append(label)
            continue
        if re.search(rf"(?<![a-z0-9]){re.escape(needle)}(?![a-z0-9])", padded):
            mentions.append(label)
    return _dedupe(mentions)


def extract_offer_terms(text_normalized: str) -> List[str]:
    terms = [label for needle, label in OFFER_TERM_HINTS.items() if needle in text_normalized]
    if _PERCENT_PATTERN.search(text_normalized):
        terms.append("percent_discount")
    if _PRICE_PATTERN.search(text_normalized):
        terms.append("fixed_price")
    return _dedupe(terms)


def infer_segment_type(text_normalized: str, prices: List[float], services: List[str], terms: List[str]) -> str:
    if any(term in terms for term in ("gift_card", "coupon", "rebate")):
        return "manufacturer_coupon"
    if "membership" in terms or "monthly" in terms:
        return "membership"
    if "package" in terms or "buy_x_get_y" in terms:
        return "package_or_promo"
    if prices and services:
        return "price_row"
    if prices:
        return "price_signal"
    if services:
        return "service_context"
    if any(term in text_normalized for term in BOILERPLATE_TERMS):
        return "boilerplate"
    return "unknown"


def build_semantic_fingerprint_parts(
    *,
    service_mentions: List[str],
    price_values: List[float],
    offer_terms: List[str],
) -> List[str]:
    price_parts = [f"price:{value:g}" for value in price_values]
    return [
        *(f"service:{value.lower()}" for value in service_mentions),
        *(f"term:{value}" for value in offer_terms),
        *price_parts,
    ]


def parse_segment(segment_index: int, text: str, *, source_url_normalized: str = "") -> ParsedSegment:
    normalized = normalize_segment_text(text)
    prices = extract_price_values(text)
    services = extract_mentions(normalized, SERVICE_HINTS)
    brands = extract_mentions(normalized, BRAND_HINTS)
    terms = extract_offer_terms(normalized)
    segment_type = infer_segment_type(normalized, prices, services, terms)
    is_price_signal = bool(prices or _PERCENT_PATTERN.search(normalized))
    is_offer_signal = bool(is_price_signal and (services or terms))
    semantic_parts = build_semantic_fingerprint_parts(
        service_mentions=services,
        price_values=prices,
        offer_terms=terms,
    )
    semantic_text = "|".join(semantic_parts) or normalized
    identity_parts = [
        source_url_normalized,
        segment_type,
        ",".join(value.lower() for value in services),
        ",".join(value for value in terms if value not in {"fixed_price", "percent_discount"}),
    ]
    non_noise_terms = [term for term in terms if term not in {"fixed_price", "percent_discount"}]
    quality = 0.0
    if is_price_signal:
        quality += 0.4
    if services:
        quality += 0.35
    if non_noise_terms:
        quality += 0.15
    if segment_type not in {"boilerplate", "unknown"}:
        quality += 0.1
    return ParsedSegment(
        segment_index=segment_index,
        text=_WHITESPACE_PATTERN.sub(" ", str(text or "")).strip(),
        text_normalized=normalized,
        text_hash=sha256_text(normalized),
        semantic_hash=sha256_text(semantic_text),
        segment_identity_hash=sha256_text("|".join(identity_parts)),
        segment_type=segment_type,
        heading_context="",
        price_values=prices,
        service_mentions=services,
        brand_mentions=brands,
        offer_terms=terms,
        is_price_signal=is_price_signal,
        is_offer_signal=is_offer_signal,
        content_quality_score=round(min(quality, 1.0), 3),
    )


def parse_page_segments(row: dict[str, Any]) -> List[ParsedSegment]:
    source_url_normalized = normalize_url(row.get("subpage_url"))
    return [
        parse_segment(index, text, source_url_normalized=source_url_normalized)
        for index, text in split_page_content(row.get("page_content"))
    ]


def build_segment_records(row: dict[str, Any]) -> List[dict[str, Any]]:
    return [segment.to_record(row) for segment in parse_page_segments(row)]


def summarize_segment_records(records: List[dict[str, Any]]) -> dict[str, Any]:
    return {
        "segment_count": len(records),
        "price_signal_count": sum(1 for item in records if item.get("is_price_signal")),
        "offer_signal_count": sum(1 for item in records if item.get("is_offer_signal")),
        "segment_types": sorted({str(item.get("segment_type")) for item in records}),
    }
