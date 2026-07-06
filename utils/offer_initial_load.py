"""Initial offer load planning from evidence-backed LLM extraction.

The functions in this module are pure. They turn normalized LLM offers and page
segment records into database-shaped rows for promo_offer_master and
promo_offer_evidence, without writing to Supabase.
"""
from __future__ import annotations

import hashlib
import json
import re
from decimal import Decimal, InvalidOperation
from typing import Any, Dict, Iterable, List, Optional

from utils.offer_evidence_segments import normalize_segment_text, normalize_url, sha256_text

_TEXT_FIELDS = [
    "service_category",
    "service_name",
    "offer_raw_text",
    "template_type",
    "unit_type",
    "start_date",
    "end_date",
    "membership_name",
    "billing_period",
    "cancellation_policy",
]
_NUMERIC_FIELDS = [
    "original_price",
    "regular_price",
    "discount_price",
    "discount_amount",
    "discount_percent",
    "membership_price",
]
_PRICE_RE = re.compile(r"\d+(?:\.\d+)?")

_CANONICAL_SERVICE_ALIASES = {
    "botox": "Botox",
    "dysport": "Dysport",
    "daxxify": "Daxxify",
    "letybo": "Letybo",
    "lip flip": "Neurotoxin",
    "neurotoxin": "Neurotoxin",
    "tox": "Neurotoxin",
    "juvederm": "Dermal Filler",
    "juvéderm": "Dermal Filler",
    "restylane": "Dermal Filler",
    "filler": "Dermal Filler",
    "fillers": "Dermal Filler",
    "morpheus": "Morpheus8",
    "morpheus8": "Morpheus8",
    "microneedling": "Microneedling",
    "membership": "Membership",
}


def _clean_text(value: Any) -> str:
    return re.sub(r"\s+", " ", str(value or "").strip())


def parse_numeric(value: Any) -> Optional[float]:
    if value in (None, ""):
        return None
    if isinstance(value, (int, float)):
        return float(value)
    text = str(value).replace(",", "")
    match = _PRICE_RE.search(text)
    if not match:
        return None
    try:
        return float(Decimal(match.group(0)))
    except (InvalidOperation, ValueError):
        return None


def _format_numeric(value: float) -> str:
    return str(int(value)) if float(value).is_integer() else str(value)


def infer_canonical_service_name(*values: Any) -> str:
    haystack = normalize_segment_text(" ".join(_clean_text(value) for value in values))
    for alias, canonical in _CANONICAL_SERVICE_ALIASES.items():
        if re.search(rf"(?<![a-z0-9]){re.escape(alias)}(?![a-z0-9])", haystack):
            return canonical
    for value in values:
        text = _clean_text(value)
        if text:
            return text
    return "Others"


def infer_offer_type(offer: Dict[str, Any]) -> str:
    joined = normalize_segment_text(
        " ".join(
            _clean_text(offer.get(field))
            for field in ("template_type", "offer_raw_text", "membership_name", "offer_content")
        )
    )
    if offer.get("membership_price") or "membership" in joined or "monthly" in joined:
        return "membership"
    if "package" in joined or "bundle" in joined or "buy " in joined:
        return "package"
    if offer.get("discount_price") or offer.get("discount_amount") or offer.get("discount_percent"):
        return "promotion"
    return "standard_price"


def infer_price_model(offer: Dict[str, Any]) -> str:
    unit = normalize_segment_text(offer.get("unit_type"))
    raw = normalize_segment_text(offer.get("offer_raw_text"))
    if "unit" in unit or "per unit" in raw or "/unit" in raw:
        return "per_unit"
    if "syringe" in unit or "syringe" in raw:
        return "per_syringe"
    if offer.get("membership_price") or "month" in unit or "monthly" in raw:
        return "monthly_membership"
    if "percent" in unit or offer.get("discount_percent"):
        return "percent_discount"
    return "fixed_price"


def build_price_signature(offer: Dict[str, Any]) -> str:
    parts: List[str] = []
    for field in _NUMERIC_FIELDS:
        parsed = parse_numeric(offer.get(field))
        if parsed is not None:
            parts.append(f"{field}:{_format_numeric(parsed)}")
    unit = _clean_text(offer.get("unit_type")).lower()
    if unit:
        parts.append(f"unit:{unit}")
    return "|".join(parts)


def build_offer_fingerprint(
    *,
    source_url_normalized: str,
    canonical_service_name: str,
    display_service_name: str,
    offer_type: str,
    price_model: str,
    price_signature: str,
) -> str:
    identity = "|".join(
        [
            source_url_normalized,
            canonical_service_name.lower(),
            normalize_segment_text(display_service_name),
            offer_type,
            price_model,
            price_signature,
        ]
    )
    return hashlib.sha256(identity.encode("utf-8")).hexdigest()


def _segment_by_index(segment_records: Iterable[Dict[str, Any]]) -> Dict[int, Dict[str, Any]]:
    out: Dict[int, Dict[str, Any]] = {}
    for item in segment_records:
        try:
            out[int(item.get("segment_index"))] = item
        except (TypeError, ValueError):
            continue
    return out


def _evidence_text_for_offer(offer: Dict[str, Any], segments_by_index: Dict[int, Dict[str, Any]]) -> str:
    evidence = []
    for index in offer.get("evidence_segments") or []:
        segment = segments_by_index.get(int(index)) if str(index).isdigit() else None
        if segment and segment.get("text"):
            evidence.append(str(segment["text"]))
    return " | ".join(evidence) or _clean_text(offer.get("offer_raw_text") or offer.get("offer_content"))


def build_master_offer_row(
    offer: Dict[str, Any],
    page_row: Dict[str, Any],
    segment_records: Iterable[Dict[str, Any]],
) -> Dict[str, Any]:
    source_url = _clean_text(page_row.get("subpage_url") or page_row.get("source_url"))
    source_url_normalized = normalize_url(source_url)
    raw_service_name = _clean_text(offer.get("raw_service_name") or offer.get("service_name"))
    display_service_name = _clean_text(offer.get("display_service_name") or raw_service_name)
    evidence_text = _evidence_text_for_offer(offer, _segment_by_index(segment_records))
    canonical_service_name = _clean_text(offer.get("canonical_service_name")) or infer_canonical_service_name(
        offer.get("service_name"),
        raw_service_name,
        offer.get("offer_raw_text"),
        evidence_text,
    )
    offer_type = _clean_text(offer.get("offer_type")) or infer_offer_type(offer)
    price_model = _clean_text(offer.get("price_model")) or infer_price_model(offer)
    price_signature = build_price_signature(offer)
    offer_fingerprint = build_offer_fingerprint(
        source_url_normalized=source_url_normalized,
        canonical_service_name=canonical_service_name,
        display_service_name=display_service_name,
        offer_type=offer_type,
        price_model=price_model,
        price_signature=price_signature,
    )

    row: Dict[str, Any] = {
        "channel": "Website",
        "status": "active",
        "lifecycle_status": "active",
        "source_url": source_url,
        "source_url_normalized": source_url_normalized,
        "source_name": _clean_text(page_row.get("domain_name") or page_row.get("source_name")),
        "source_website_id": page_row.get("promo_website_id"),
        "business_id": page_row.get("business_id"),
        "raw_service_name": raw_service_name,
        "display_service_name": display_service_name,
        "canonical_service_name": canonical_service_name,
        "service_name": canonical_service_name,
        "offer_type": offer_type,
        "price_model": price_model,
        "offer_fingerprint": offer_fingerprint,
        "price_signature": price_signature,
        "identity_confidence": 0.85 if price_signature and canonical_service_name != "Others" else 0.55,
        "evidence_hash": sha256_text(normalize_segment_text(evidence_text)),
        "moderation_status": "approved",
    }

    for field in _TEXT_FIELDS:
        value = _clean_text(offer.get(field))
        if value:
            row[field] = value
    row["service_name"] = canonical_service_name

    for field in _NUMERIC_FIELDS:
        value = parse_numeric(offer.get(field))
        if value is not None:
            row[field] = value

    offer_content = offer.get("offer_content")
    if isinstance(offer_content, (dict, list)):
        row["offer_content"] = offer_content
    elif _clean_text(offer_content):
        parsed = None
        try:
            parsed = json.loads(str(offer_content))
        except json.JSONDecodeError:
            parsed = None
        row["offer_content"] = parsed if parsed is not None else _clean_text(offer_content)

    return {key: value for key, value in row.items() if value not in (None, "")}


def build_offer_evidence_rows(
    offer: Dict[str, Any],
    master_row: Dict[str, Any],
    segment_records: Iterable[Dict[str, Any]],
) -> List[Dict[str, Any]]:
    segments = _segment_by_index(segment_records)
    rows: List[Dict[str, Any]] = []
    for index in offer.get("evidence_segments") or []:
        segment = segments.get(int(index)) if str(index).isdigit() else None
        if not segment:
            continue
        rows.append(
            {
                "offer_fingerprint": master_row.get("offer_fingerprint"),
                "segment_identity_hash": segment.get("segment_identity_hash"),
                "promo_website_id": segment.get("promo_website_id"),
                "evidence_role": "primary_offer_text",
                "evidence_text": segment.get("text") or "",
                "evidence_hash": segment.get("text_hash") or sha256_text(normalize_segment_text(segment.get("text"))),
                "confidence": master_row.get("identity_confidence"),
                "status": "active",
            }
        )
    return rows


def plan_initial_offer_load(
    page_row: Dict[str, Any],
    offers: Iterable[Dict[str, Any]],
    segment_records: Iterable[Dict[str, Any]],
) -> Dict[str, Any]:
    segment_list = list(segment_records)
    master_rows: List[Dict[str, Any]] = []
    evidence_rows: List[Dict[str, Any]] = []
    seen: set[str] = set()
    duplicates: List[Dict[str, Any]] = []

    for offer in offers:
        if not isinstance(offer, dict):
            continue
        master = build_master_offer_row(offer, page_row, segment_list)
        fingerprint = str(master.get("offer_fingerprint") or "")
        if fingerprint in seen:
            duplicates.append({"offer_fingerprint": fingerprint, "offer_raw_text": master.get("offer_raw_text")})
            continue
        seen.add(fingerprint)
        master_rows.append(master)
        evidence_rows.extend(build_offer_evidence_rows(offer, master, segment_list))

    return {
        "promo_website_id": page_row.get("promo_website_id"),
        "source_url": _clean_text(page_row.get("subpage_url") or page_row.get("source_url")),
        "source_url_normalized": normalize_url(page_row.get("subpage_url") or page_row.get("source_url")),
        "master_rows": master_rows,
        "evidence_rows": evidence_rows,
        "duplicate_offers": duplicates,
        "summary": {
            "offers_input": len([item for item in offers if isinstance(item, dict)]),
            "master_rows": len(master_rows),
            "evidence_rows": len(evidence_rows),
            "duplicate_offers": len(duplicates),
        },
    }