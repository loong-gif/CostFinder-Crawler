"""
Change-driven offer extraction from Firecrawl monitor diff data.

Extracts offers directly from the structured diff (diff.json, diff.text,
judgment.meaningfulChanges) that Firecrawl already computes on each monitor
check, bypassing the full-page Apify recrawl + full-content LLM pipeline.

Token cost comparison:
  Old path  full page_content (2000–8000 tok) × 2 LLM calls (select + extract)
  New path  focused diff payload (200–600 tok) × 1 LLM call
"""
from __future__ import annotations

import json
import re
import uuid
from pathlib import Path
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

from utils.logger import log
from utils.offer_scope_filter import should_exclude_from_offer_master
from utils.service_category_lookup import MASTER_CATEGORY_PROMPT, resolve_service_category
from utils.offer_extraction_llm import (
    OFFER_OUTPUT_FIELDS,
    StructuredLLMClient,
    canonicalize_service_name,
    get_standardized_service_names,
    normalize_offer_record,
    parse_json_payload,
)
from utils.offer_evidence_segments import normalize_url
from utils.clinic_promotions_db import fetch_promotion_by_url, upsert_promotion
from utils.offer_fingerprint import compute_offer_fingerprint
from utils.offer_field_normalize import normalize_offer_field_values
from utils.offer_price_normalize import normalize_offer_prices, parse_price
from utils.promo_offer_items_db import build_item_from_offer_fields
from utils.schema_contract import offer_item_name, TABLE_PROMO_OFFER_MASTER

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

_MAX_TEXT_DIFF_CHARS = 3000
_MAX_CANDIDATE_OFFERS = 100
_MAX_CANDIDATE_TEXT_CHARS = 200
_CONF_RANK = {"low": 1, "medium": 2, "high": 3}
_VALID_ACTIONS = {"update", "insert", "mark_ended"}
_CHANGE_EVENT_ACTIONS = {
    "update": "update_offer",
    "insert": "insert_offer",
    "mark_ended": "mark_missing",
}
_RPC_PERSIST_CHANGE_EVENTS = "persist_promo_offer_change_events"
_RPC_APPLY_OFFER_ACTION = "apply_promo_change_offer_action"
_change_driven_rpc_verified = False
_CONFIDENCE_NUMERIC = {"low": 0.35, "medium": 0.65, "high": 0.9}
_CANDIDATE_FETCH_VARIANTS = [
    (
        "id,offer_raw_text,regular_price,discount_price,promo_offer_items(item_name,unit_type)",
        "created_at.desc",
    ),
    (
        "id,offer_raw_text,regular_price,discount_price",
        "created_at.desc",
    ),
    (
        "id,offer_raw_text,discount_price",
        "created_at.desc",
    ),
    (
        "id,offer_raw_text",
        "created_at.desc",
    ),
]

CHANGE_EXTRACTION_SYSTEM_PROMPT = (
    "You extract aesthetic service offers from website change data. "
    "You receive structured diff data showing what changed on a medspa or aesthetics website, "
    "plus existing active offers already stored for this exact page. "
    "Extract ONLY offers affected by this change. "
    "Return one of three actions per offer: update, insert, or mark_ended. "
    "Service and member pricing use discount_price (and regular_price when applicable); "
    "do not output membership_name, membership_price, or billing_period — membership plan "
    "structure is stored separately in clinic_memberships. "
    "Do not insert membership tier plan fees as service_name=Membership offers in master. "
    "Do not extract free consultations or consultation-only bookings as offers. "
    "Do not extract retail skincare/catalog shop products (/collections, /shop) as treatment offers. "
    "Use update when the diff changes an existing stored offer and matched_candidate_index must point to one provided candidate. "
    "Use insert when the diff adds a brand-new offer that does not match any candidate. "
    "Use mark_ended when the diff shows an existing stored offer was removed or ended; matched_candidate_index is required and all other fields may be empty strings. "
    "Do not generate database ids. Only select from the provided candidate indexes. "
    "When pricing, dates, membership terms, or unit details are reasonably supported by the diff or candidate context, fill the structured fields instead of leaving them blank. "
    "If a pricing block contains multiple offers, split them into separate records. "
    "Always set service_category to one of: "
    f"{MASTER_CATEGORY_PROMPT}. "
    "Return strict JSON with a single top-level key 'offers'."
)

# Fields from OFFER_OUTPUT_FIELDS that map directly to promo_offer_master columns.
# (offer_content and evidence_segments are internal and have no master column.)
_MASTER_TEXT_FIELDS = [
    "service_category",
    "offer_raw_text",
]
_ITEM_SOURCE_FIELDS = ("service_name", "unit_type", "service_area", "quantity")
_MASTER_NUMERIC_FIELDS = [
    "regular_price",
    "discount_price",
    "discount_amount",
    "discount_percent",
]
_CHANGE_EXTRACTION_EXTRA_FIELDS = [
    field
    for field in _MASTER_TEXT_FIELDS + _MASTER_NUMERIC_FIELDS
    if field not in OFFER_OUTPUT_FIELDS
]
_CHANGE_EXTRACTION_FIELDS = [
    "action",
    "matched_id",
    "matched_candidate_index",
    "raw_service_name",
    *OFFER_OUTPUT_FIELDS,
    *_CHANGE_EXTRACTION_EXTRA_FIELDS,
]
_CHANGE_SCHEMA_FIELDS = ", ".join(_CHANGE_EXTRACTION_FIELDS)


def build_change_extraction_json_schema() -> Dict[str, Any]:
    offer_properties: Dict[str, Any] = {
        field: {"type": "string"} for field in _CHANGE_EXTRACTION_FIELDS
    }
    offer_properties["action"] = {
        "type": "string",
        "enum": ["update", "insert", "mark_ended"],
    }
    return {
        "type": "object",
        "properties": {
            "offers": {
                "type": "array",
                "items": {
                    "type": "object",
                    "properties": offer_properties,
                    "required": ["action"],
                    "additionalProperties": False,
                },
            }
        },
        "required": ["offers"],
        "additionalProperties": False,
    }


# ---------------------------------------------------------------------------
# Diff payload extraction
# ---------------------------------------------------------------------------

def _head_tail(text: str, limit: int) -> str:
    if len(text) <= limit:
        return text
    half = limit // 2
    return text[:half] + "\n...[truncated middle]...\n" + text[-half:]


def _normalize_page_diff_fields(page: Dict[str, Any]) -> Dict[str, Any]:
    diff = page.get("diff") or {}
    if not isinstance(diff, dict):
        diff = {}

    judgment = page.get("judgment") or {}
    if not isinstance(judgment, dict):
        judgment = {}

    text_diff = (diff.get("text") or "").strip()
    json_diff = diff.get("json") or {}
    if not isinstance(json_diff, dict):
        json_diff = {}

    meaningful_changes = (
        judgment.get("meaningfulChanges")
        or judgment.get("meaningful_changes")
        or []
    )
    if not isinstance(meaningful_changes, list):
        meaningful_changes = []

    return {
        "text_diff": text_diff,
        "json_diff": json_diff,
        "meaningful_changes": meaningful_changes,
        "judgment_reason": (judgment.get("reason") or "").strip(),
        "confidence": (judgment.get("confidence") or "").strip().lower(),
    }


def _build_diff_payload(page: Dict[str, Any], fields: Dict[str, Any]) -> Dict[str, Any]:
    payload: Dict[str, Any] = {
        "url": (page.get("url") or "").strip(),
        "status": (page.get("status") or "").strip(),
        "text_diff": _head_tail(fields["text_diff"], _MAX_TEXT_DIFF_CHARS)
        if fields["text_diff"]
        else "",
        "json_diff": fields["json_diff"],
        "meaningful_changes": fields["meaningful_changes"],
        "judgment_reason": fields["judgment_reason"],
        "confidence": fields["confidence"],
    }
    if page.get("business_id") is not None:
        payload["business_id"] = page["business_id"]
    return payload


def extract_diff_payload(page: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    """Parse a Firecrawl check page into a compact diff payload.

    Returns None when no usable diff data is present, signalling that this
    page needs the Apify fallback.
    """
    fields = _normalize_page_diff_fields(page)
    if (
        not fields["text_diff"]
        and not fields["json_diff"]
        and not fields["meaningful_changes"]
    ):
        return None
    return _build_diff_payload(page, fields)


# ---------------------------------------------------------------------------
# Candidate offer fetching / prompt construction
# ---------------------------------------------------------------------------

def _truncate_text(value: Any, limit: int) -> str:
    text = str(value or "").strip()
    if len(text) <= limit:
        return text
    return text[: limit - 3].rstrip() + "..."


def fetch_candidate_offers(
    client: Any,
    source_url: str,
    *,
    limit: int = _MAX_CANDIDATE_OFFERS,
) -> List[Dict[str, Any]]:
    """Fetch active master offers for a page and compress them for LLM context."""
    promotion = fetch_promotion_by_url(client, source_url)
    if not promotion:
        return []

    last_error: Optional[Exception] = None
    rows: List[Dict[str, Any]] = []
    for select, order in _CANDIDATE_FETCH_VARIANTS:
        try:
            rows = client.fetch_rows(
                TABLE_PROMO_OFFER_MASTER,
                select,
                filters={
                    "promotion_id": f"eq.{promotion['promotion_id']}",
                    "is_active": "eq.true",
                },
                limit=limit + 1,
                order=order,
            )
            break
        except Exception as exc:
            last_error = exc
    else:
        if last_error is not None:
            raise last_error

    truncated = rows[:limit]
    if len(rows) > limit:
        log.warning(
            "change_driven: candidate offers truncated for {url} to {limit} rows",
            url=source_url,
            limit=limit,
        )

    candidates: List[Dict[str, Any]] = []
    for idx, row in enumerate(truncated, start=1):
        if not isinstance(row, dict):
            continue
        candidate = {
            "id": str(row.get("id") or "").strip(),
            "candidate_index": idx,
            "service_name": offer_item_name(row),
            "offer_raw_text": _truncate_text(
                row.get("offer_raw_text"), _MAX_CANDIDATE_TEXT_CHARS
            ),
            "regular_price": row.get("regular_price"),
            "discount_price": row.get("discount_price"),
            "original_price": row.get("regular_price"),
        }
        if candidate["id"]:
            candidates.append(candidate)
    return candidates


def _reindex(candidate: Dict[str, Any], new_index: int) -> Dict[str, Any]:
    out = dict(candidate)
    out["candidate_index"] = new_index
    return out


def _candidate_diff_score(candidate: Dict[str, Any], diff_tokens: set) -> int:
    cand_text = _normalize_match_text(
        str(candidate.get("service_name") or "")
        + " "
        + str(candidate.get("offer_raw_text") or "")
    )
    if not cand_text or not diff_tokens:
        return 0
    cand_tokens = set(cand_text.split())
    return len(cand_tokens & diff_tokens)


def filter_candidates_by_diff_relevance(
    candidates: List[Dict[str, Any]],
    meaningful_changes: List[Dict[str, Any]],
    *,
    max_keep: int = 10,
) -> List[Dict[str, Any]]:
    """Pre-filter candidate offers by relevance to meaningful_changes text."""
    if not candidates:
        return []
    if len(candidates) <= max_keep:
        return [_reindex(candidate, idx) for idx, candidate in enumerate(candidates, start=1)]

    diff_text = " ".join(
        str(item.get("before") or "") + " " + str(item.get("after") or "")
        for item in meaningful_changes
        if isinstance(item, dict)
    )
    diff_tokens = set(_normalize_match_text(diff_text).split())

    scored = [
        (_candidate_diff_score(candidate, diff_tokens), idx, candidate)
        for idx, candidate in enumerate(candidates)
    ]
    scored.sort(key=lambda item: (-item[0], item[1]))

    kept = [candidate for _, _, candidate in scored[:max_keep]]
    return [_reindex(candidate, idx) for idx, candidate in enumerate(kept, start=1)]


def build_change_extraction_messages(
    payload: Dict[str, Any],
    domain_name: str,
    candidate_offers: List[Dict[str, Any]],
) -> List[Dict[str, str]]:
    """Build a focused single-stage LLM prompt for change-driven extraction."""
    parts: List[str] = []

    if payload["judgment_reason"]:
        parts.append(f"Change summary: {payload['judgment_reason']}")

    if payload["meaningful_changes"]:
        parts.append(
            "Detected changes:\n"
            + json.dumps(payload["meaningful_changes"], ensure_ascii=False, indent=2)
        )

    if payload["json_diff"]:
        parts.append(
            "Structured diff (before → after):\n"
            + json.dumps(payload["json_diff"], ensure_ascii=False, indent=2)
        )

    if payload["text_diff"]:
        parts.append("Text diff:\n" + payload["text_diff"])

    if candidate_offers:
        prompt_candidates = [
            {
                "candidate_index": item.get("candidate_index"),
                "service_name": item.get("service_name"),
                "offer_raw_text": item.get("offer_raw_text"),
                "regular_price": item.get("regular_price"),
                "discount_price": item.get("discount_price"),
                "original_price": item.get("original_price"),
            }
            for item in candidate_offers
        ]
        candidates_block = json.dumps(prompt_candidates, ensure_ascii=False, indent=2)
    else:
        candidates_block = "(no existing offers)"

    change_body = "\n\n".join(parts)
    allowed_service_names = json.dumps(get_standardized_service_names(), ensure_ascii=False)

    user_content = (
        f"Domain: {domain_name}\n"
        f"Page: {payload['url']}\n\n"
        f"{change_body}\n\n"
        "Existing offers in database for this page (candidates):\n"
        f"{candidates_block}\n\n"
        "Extract affected offers only.\n"
        f"Required fields per offer: {_CHANGE_SCHEMA_FIELDS}.\n"
        f"Allowed canonical service_name values: {allowed_service_names}.\n"
        "Rules:\n"
        "- update requires matched_candidate_index from the candidate list.\n"
        "- insert must use an empty matched_candidate_index.\n"
        "- mark_ended requires matched_candidate_index from the candidate list and other fields may be empty strings.\n"
        "- never output raw database ids.\n"
        "- raw_service_name should capture the source wording before normalization.\n"
        "- service_name should be the best canonical label chosen from the allowed list.\n"
        '- Populate as many structured fields as the diff and candidate context support. Return JSON: {"offers": [...]}'
    )

    return [
        {"role": "system", "content": CHANGE_EXTRACTION_SYSTEM_PROMPT},
        {"role": "user", "content": user_content},
    ]


# ---------------------------------------------------------------------------
# Offer normalisation / validation
# ---------------------------------------------------------------------------

def _parse_price(value: Any) -> Optional[float]:
    if value is None or value == "":
        return None
    cleaned = re.sub(r"[,$]", "", str(value)).strip()
    try:
        return float(cleaned)
    except (ValueError, TypeError):
        return None


def _format_price_for_offer(value: float) -> str:
    if float(value).is_integer():
        return str(int(value))
    return str(value)


def _normalize_match_text(value: Any) -> str:
    text = str(value or "").strip().lower()
    text = re.sub(r"[^a-z0-9]+", " ", text)
    return re.sub(r"\s+", " ", text).strip()


def _extract_price_from_text(value: Any) -> Optional[float]:
    text = str(value or "")
    match = re.search(r"\$?\s*(\d+(?:\.\d+)?)\s*(?:/|per)\s*[a-z]+", text, re.IGNORECASE)
    if match:
        return _parse_price(match.group(1))
    match = re.search(r"\$?\s*(\d+(?:\.\d+)?)", text)
    if match:
        return _parse_price(match.group(1))
    return None


def _extract_offer_price_fields(value: Any) -> Dict[str, Optional[float]]:
    if not isinstance(value, dict):
        return {"regular_price": None, "discount_price": None}

    regular_price = (
        _parse_price(value.get("regular_price"))
        or _parse_price(value.get("original_price"))
    )
    discount_price = _parse_price(value.get("discount_price"))

    text_price = _extract_price_from_text(value.get("offer_raw_text"))
    if discount_price is None:
        discount_price = text_price
    elif regular_price is None and text_price is not None and text_price != discount_price:
        regular_price = text_price

    return {
        "regular_price": regular_price,
        "discount_price": discount_price,
    }


def _iter_changed_offer_pairs(json_diff: Any) -> List[Dict[str, Any]]:
    pairs: List[Dict[str, Any]] = []

    def walk(node: Any) -> None:
        if isinstance(node, dict):
            if "previous" in node or "current" in node:
                pairs.append(
                    {
                        "previous": node.get("previous"),
                        "current": node.get("current"),
                    }
                )
            for value in node.values():
                walk(value)
        elif isinstance(node, list):
            for item in node:
                walk(item)

    walk(json_diff)
    return pairs


def _offer_match_targets(
    offer: Dict[str, Any],
    candidate: Dict[str, Any],
) -> tuple[set[str], set[str]]:
    target_names = {
        _normalize_match_text(offer.get("service_name")),
        _normalize_match_text(candidate.get("service_name")),
    }
    target_names = {name for name in target_names if name}

    target_texts = {
        _normalize_match_text(offer.get("offer_raw_text")),
        _normalize_match_text(candidate.get("offer_raw_text")),
    }
    target_texts = {text for text in target_texts if text}
    return target_names, target_texts


def _side_matches_offer_change(
    side: Any,
    *,
    target_names: set[str],
    target_texts: set[str],
) -> bool:
    if not isinstance(side, dict):
        return False
    side_name = _normalize_match_text(side.get("service_name"))
    side_text = _normalize_match_text(side.get("offer_raw_text"))

    if side_name and any(
        side_name == target or side_name in target or target in side_name
        for target in target_names
    ):
        return True
    if side_text and any(
        side_text == target or side_text in target or target in side_text
        for target in target_texts
    ):
        return True
    return False


def _offer_change_matches(
    previous: Any,
    current: Any,
    *,
    offer: Dict[str, Any],
    candidate: Dict[str, Any],
) -> bool:
    target_names, target_texts = _offer_match_targets(offer, candidate)
    return _side_matches_offer_change(
        previous, target_names=target_names, target_texts=target_texts
    ) or _side_matches_offer_change(
        current, target_names=target_names, target_texts=target_texts
    )


def _find_matching_changed_pair(
    offer: Dict[str, Any],
    candidate: Dict[str, Any],
    changed_pairs: List[Dict[str, Any]],
) -> Optional[Dict[str, Any]]:
    for pair in changed_pairs:
        if _offer_change_matches(
            pair.get("previous"),
            pair.get("current"),
            offer=offer,
            candidate=candidate,
        ):
            return pair
    return None


def _resolve_update_prices_from_pair(
    offer: Dict[str, Any],
    candidate: Dict[str, Any],
    pair: Dict[str, Any],
) -> Dict[str, Any]:
    current_regular = _parse_price(offer.get("regular_price"))
    current_discount = _parse_price(offer.get("discount_price"))
    if current_regular is not None and current_discount is not None:
        return offer

    previous_prices = _extract_offer_price_fields(pair.get("previous"))
    current_prices = _extract_offer_price_fields(pair.get("current"))

    if current_discount is None:
        current_discount = (
            current_prices["discount_price"] or current_prices["regular_price"]
        )
        if current_discount is None:
            current_discount = (
                _parse_price(candidate.get("discount_price"))
                or _parse_price(candidate.get("regular_price"))
                or _parse_price(candidate.get("original_price"))
            )

    if current_regular is None:
        current_regular = (
            previous_prices["regular_price"] or previous_prices["discount_price"]
        )
        if current_regular is None:
            current_regular = (
                _parse_price(candidate.get("regular_price"))
                or _parse_price(candidate.get("discount_price"))
                or _parse_price(candidate.get("original_price"))
            )

    enriched_offer = dict(offer)
    if current_regular is not None:
        enriched_offer["regular_price"] = _format_price_for_offer(current_regular)
    if current_discount is not None:
        enriched_offer["discount_price"] = _format_price_for_offer(current_discount)
    return enriched_offer


def _enrich_single_update_offer_with_diff_prices(
    offer: Dict[str, Any],
    changed_pairs: List[Dict[str, Any]],
    candidate_by_id: Dict[str, Dict[str, Any]],
) -> Dict[str, Any]:
    if str(offer.get("action") or "").strip().lower() != "update":
        return offer

    matched_id = str(offer.get("matched_id") or "").strip()
    candidate = candidate_by_id.get(matched_id, {})
    if not candidate:
        return offer

    current_regular = _parse_price(offer.get("regular_price"))
    current_discount = _parse_price(offer.get("discount_price"))
    if current_regular is not None and current_discount is not None:
        return offer

    best_pair = _find_matching_changed_pair(offer, candidate, changed_pairs)
    if best_pair is None:
        return offer

    return _resolve_update_prices_from_pair(offer, candidate, best_pair)


def enrich_update_actions_with_diff_prices(
    offers: List[Dict[str, Any]],
    payload: Dict[str, Any],
    candidate_offers: List[Dict[str, Any]],
) -> List[Dict[str, Any]]:
    """Backfill update prices from structured diff when the model leaves them blank."""
    if not offers:
        return offers

    candidate_by_id = {
        str(item.get("id") or "").strip(): item
        for item in candidate_offers
        if isinstance(item, dict) and str(item.get("id") or "").strip()
    }
    changed_pairs = _iter_changed_offer_pairs(payload.get("json_diff") or {})
    if not changed_pairs:
        return offers

    return [
        _enrich_single_update_offer_with_diff_prices(offer, changed_pairs, candidate_by_id)
        for offer in offers
    ]


def _standardize_single_offer_service_name(
    offer: Dict[str, Any],
    candidate_by_id: Dict[str, Dict[str, Any]],
) -> Dict[str, Any]:
    action = str(offer.get("action") or "").strip().lower()
    if action == "mark_ended":
        return offer

    matched_id = str(offer.get("matched_id") or "").strip()
    candidate = candidate_by_id.get(matched_id, {})
    standardized_offer = dict(offer)
    raw_service_name = str(
        offer.get("raw_service_name") or offer.get("service_name") or ""
    ).strip()
    standardized_offer["raw_service_name"] = raw_service_name
    standardized_offer["service_name"] = canonicalize_service_name(
        offer.get("service_name"),
        raw_service_name,
        offer.get("offer_raw_text"),
        offer.get("offer_content"),
        candidate.get("service_name"),
        candidate.get("offer_raw_text"),
    )
    return standardized_offer


def standardize_offer_service_names(
    offers: List[Dict[str, Any]],
    candidate_offers: List[Dict[str, Any]],
) -> List[Dict[str, Any]]:
    """Force service_name to come from the standardized dictionary, not raw model text."""
    if not offers:
        return offers

    candidate_by_id = {
        str(item.get("id") or "").strip(): item
        for item in candidate_offers
        if isinstance(item, dict) and str(item.get("id") or "").strip()
    }

    return [
        _standardize_single_offer_service_name(offer, candidate_by_id) for offer in offers
    ]


def normalize_change_offer_record(record: Dict[str, Any]) -> Dict[str, Any]:
    normalized = normalize_offer_record(record, allowed_indexes=set())
    normalized["raw_service_name"] = str(record.get("raw_service_name") or "").strip()
    for field in _CHANGE_EXTRACTION_EXTRA_FIELDS:
        value = record.get(field, "")
        if value is None:
            value = ""
        if isinstance(value, (dict, list)):
            normalized[field] = json.dumps(value, ensure_ascii=False, separators=(",", ":"))
        else:
            normalized[field] = str(value).strip()

    normalized["action"] = str(record.get("action") or "").strip().lower()
    normalized["matched_id"] = str(record.get("matched_id") or "").strip()
    normalized["matched_candidate_index"] = str(
        record.get("matched_candidate_index") or ""
    ).strip()
    return normalized


def _build_mark_ended_offer(
    matched_id: str,
    matched_candidate_index: str,
) -> Dict[str, Any]:
    mark_ended_offer = {field: "" for field in _CHANGE_EXTRACTION_FIELDS}
    mark_ended_offer["action"] = "mark_ended"
    mark_ended_offer["matched_id"] = matched_id
    mark_ended_offer["matched_candidate_index"] = matched_candidate_index
    return mark_ended_offer


def _validate_single_offer_action(
    raw_offer: Dict[str, Any],
    *,
    candidate_ids: set[str],
    candidate_id_by_index: Dict[str, str],
    source_url: str,
    candidates_unavailable: bool,
) -> tuple[Optional[Dict[str, Any]], int, int]:
    """Return (validated_offer_or_none, downgraded_delta, skipped_delta)."""
    raw_has_content = any(
        str(raw_offer.get(field) or "").strip()
        for field in ("service_name", "raw_service_name", "offer_raw_text", "offer_content")
    )

    offer = normalize_change_offer_record(raw_offer)
    action = offer["action"]
    matched_id = offer["matched_id"]
    matched_candidate_index = offer["matched_candidate_index"]
    downgraded = 0
    skipped = 0

    if matched_candidate_index and matched_candidate_index in candidate_id_by_index:
        matched_id = candidate_id_by_index[matched_candidate_index]

    if action not in _VALID_ACTIONS:
        downgraded += 1
        log.warning(
            "change_driven: invalid action '{action}' for {url}, downgrading to insert",
            action=action or "<empty>",
            url=source_url or "<unknown>",
        )
        action = "insert"
        matched_id = ""
        matched_candidate_index = ""

    if candidates_unavailable and action != "insert":
        downgraded += 1
        log.warning(
            "change_driven: candidates unavailable for {url}, forcing action {action} -> insert",
            url=source_url or "<unknown>",
            action=action,
        )
        action = "insert"
        matched_id = ""
        matched_candidate_index = ""

    if action in {"update", "mark_ended"}:
        if not matched_id:
            downgraded += 1
            if action == "update":
                log.warning(
                    "change_driven: update missing matched_id for {url}, downgrading to insert",
                    url=source_url or "<unknown>",
                )
                action = "insert"
                matched_candidate_index = ""
            else:
                log.warning(
                    "change_driven: mark_ended missing matched_id for {url}, skipping",
                    url=source_url or "<unknown>",
                )
                skipped += 1
                return None, downgraded, skipped

        elif matched_id not in candidate_ids:
            downgraded += 1
            if action == "update":
                log.warning(
                    "change_driven: update matched_id={mid} not found for {url}, downgrading to insert",
                    mid=matched_id,
                    url=source_url or "<unknown>",
                )
                action = "insert"
                matched_candidate_index = ""
            else:
                log.warning(
                    "change_driven: mark_ended matched_id={mid} not found for {url}, skipping",
                    mid=matched_id,
                    url=source_url or "<unknown>",
                )
                skipped += 1
                return None, downgraded, skipped

    if action == "insert":
        matched_id = ""
        matched_candidate_index = ""
        if not raw_has_content:
            skipped += 1
            return None, downgraded, skipped

    if action == "mark_ended":
        return (
            _build_mark_ended_offer(matched_id, matched_candidate_index),
            downgraded,
            skipped,
        )

    offer["action"] = action
    offer["matched_id"] = matched_id
    offer["matched_candidate_index"] = matched_candidate_index
    return offer, downgraded, skipped


def validate_offer_actions(
    payload: Any,
    candidate_offers: List[Dict[str, Any]],
    *,
    source_url: str = "",
    candidates_unavailable: bool = False,
) -> Dict[str, Any]:
    """Validate action payload and downgrade invalid matches safely."""
    data = parse_json_payload(payload, {})
    offers = data.get("offers", []) if isinstance(data, dict) else []
    if not isinstance(offers, list):
        offers = []

    candidate_ids = {
        str(item.get("id") or "").strip()
        for item in candidate_offers
        if isinstance(item, dict) and str(item.get("id") or "").strip()
    }
    candidate_id_by_index = {
        str(item.get("candidate_index") or "").strip(): str(item.get("id") or "").strip()
        for item in candidate_offers
        if isinstance(item, dict)
        and str(item.get("candidate_index") or "").strip()
        and str(item.get("id") or "").strip()
    }

    validated: List[Dict[str, Any]] = []
    downgraded = 0
    skipped = 0

    for raw_offer in offers:
        if not isinstance(raw_offer, dict):
            skipped += 1
            continue

        offer, offer_downgraded, offer_skipped = _validate_single_offer_action(
            raw_offer,
            candidate_ids=candidate_ids,
            candidate_id_by_index=candidate_id_by_index,
            source_url=source_url,
            candidates_unavailable=candidates_unavailable,
        )
        downgraded += offer_downgraded
        skipped += offer_skipped
        if offer is not None:
            validated.append(offer)

    return {"offers": validated, "downgraded": downgraded, "skipped": skipped}


def build_offer_update_payload(offer: Dict[str, Any]) -> Dict[str, Any]:
    payload: Dict[str, Any] = {}

    for field in _MASTER_TEXT_FIELDS:
        value = str(offer.get(field) or "").strip()
        if value:
            payload[field] = value

    prices = normalize_offer_prices(
        regular_price=offer.get("regular_price"),
        discount_price=offer.get("discount_price"),
        discount_amount=offer.get("discount_amount"),
        discount_percent=offer.get("discount_percent"),
        original_price=offer.get("original_price"),
        offer_raw_text=offer.get("offer_raw_text") or payload.get("offer_raw_text"),
    )
    for field in _MASTER_NUMERIC_FIELDS:
        value = prices.get(field)
        if value is not None:
            payload[field] = value

    payload = normalize_offer_field_values(payload, offer=offer)

    service_name = str(offer.get("service_name") or "").strip()
    if service_name:
        category, _, confidence = resolve_service_category(
            service_name,
            offer.get("service_category") or payload.get("service_category"),
            min_confidence="medium",
        )
        if category and confidence in {"high", "medium"}:
            payload["service_category"] = category

    if offer.get("is_membership_required") is not None:
        payload["is_membership_required"] = bool(offer.get("is_membership_required"))
    if offer.get("is_new_customer_required") is not None:
        payload["is_new_customer_required"] = bool(offer.get("is_new_customer_required"))

    return payload


def build_offer_item_payload(offer: Dict[str, Any]) -> Dict[str, Any]:
    return build_item_from_offer_fields(offer)


def build_offer_insert_payload(
    offer: Dict[str, Any],
    *,
    source_url: str,
    source_name: str,
    business_id: Any = None,
    promotion_id: Any = None,
) -> Dict[str, Any]:
    payload = build_offer_update_payload(offer)
    payload.update(
        {
            "is_active": True,
            "is_new_customer_required": payload.get(
                "is_new_customer_required",
                offer.get("is_new_customer_required", True),
            ),
        }
    )
    if business_id is not None:
        payload["business_id"] = business_id
    if promotion_id is not None:
        payload["promotion_id"] = promotion_id
    service_name = str(offer.get("service_name") or "").strip()
    unit_type = offer.get("unit_type")
    fingerprint = compute_offer_fingerprint(
        source_url=source_url,
        service_name=service_name,
        unit_type=unit_type,
    )
    payload["offer_fingerprint"] = fingerprint
    return payload


def find_active_offer_by_fingerprint(
    client: Any,
    *,
    business_id: Any,
    offer_fingerprint: str,
) -> Optional[str]:
    if business_id is None or not offer_fingerprint:
        return None
    try:
        rows = client.fetch_rows(
            TABLE_PROMO_OFFER_MASTER,
            "id",
            filters={
                "business_id": f"eq.{business_id}",
                "is_active": "eq.true",
                "offer_fingerprint": f"eq.{offer_fingerprint}",
            },
            limit=1,
        )
    except Exception as exc:
        response_text = getattr(getattr(exc, "response", None), "text", "")
        error_text = f"{exc} {response_text}".strip()
        if "offer_fingerprint" in error_text and (
            "does not exist" in error_text or "schema cache" in error_text
        ):
            return None
        log.warning(
            "change_driven: fingerprint lookup failed for business_id={bid}: {error}",
            bid=business_id,
            error=exc,
        )
        return None
    if not rows:
        return None
    return str(rows[0].get("id") or "").strip() or None


def infer_business_change_type(offer: Dict[str, Any]) -> str:
    """Classify an LLM action into a business-facing change category."""
    action = str(offer.get("action") or "").strip().lower()
    if action == "insert":
        return "offer_added"
    if action == "mark_ended":
        return "offer_missing"

    price_fields = (
        "regular_price",
        "discount_price",
        "discount_amount",
        "discount_percent",
    )
    if any(_parse_price(offer.get(field)) is not None for field in price_fields):
        return "price_changed"

    eligibility_fields = (
        "start_date",
        "end_date",
        "cancellation_policy",
        "unit_type",
    )
    if any(str(offer.get(field) or "").strip() for field in eligibility_fields):
        return "eligibility_changed"
    return "unknown"


def _selected_match_candidate_payload(
    offer: Dict[str, Any],
    candidate_offers: List[Dict[str, Any]],
    *,
    event_index: int,
) -> Optional[Dict[str, Any]]:
    matched_id = str(offer.get("matched_id") or "").strip()
    if not matched_id:
        return None

    matched_candidate = next(
        (
            item
            for item in candidate_offers
            if str(item.get("id") or "").strip() == matched_id
        ),
        None,
    )
    if not matched_candidate:
        return None

    rank = offer.get("matched_candidate_index") or matched_candidate.get("candidate_index")
    try:
        rank_value = int(rank)
    except (TypeError, ValueError):
        rank_value = None

    return {
        "event_index": event_index,
        "segment_id": None,
        "candidate_offer_id": matched_id,
        "match_score": 1.0,
        "match_method": "llm_selected_candidate",
        "score_breakdown": {"llm_selected": 1.0},
        "rank": rank_value,
        "is_selected": True,
    }


def build_change_event_payloads(
    offers: List[Dict[str, Any]],
    diff_payload: Dict[str, Any],
    candidate_offers: List[Dict[str, Any]],
    *,
    source_url: str,
    source_name: str,
) -> Dict[str, Any]:
    """Build auditable change-event payloads without writing to Supabase.

    The event table is intentionally a staging/audit layer. Destructive model
    actions such as mark_ended become mark_missing here so later validation can
    require repeated absence before ending a master offer.
    """
    source_url_normalized = normalize_url(source_url)
    confidence_label = str(diff_payload.get("confidence") or "").strip().lower()
    confidence = _CONFIDENCE_NUMERIC.get(confidence_label)
    reason = str(diff_payload.get("judgment_reason") or "").strip()

    events: List[Dict[str, Any]] = []
    match_candidates: List[Dict[str, Any]] = []
    for index, offer in enumerate(offers, start=1):
        action = str(offer.get("action") or "insert").strip().lower()
        proposed_action = _CHANGE_EVENT_ACTIONS.get(action, "insert_offer")
        matched_id = str(offer.get("matched_id") or "").strip()

        event = {
            "event_index": index,
            "promo_website_id": diff_payload.get("promo_website_id"),
            "source_url": source_url,
            "source_url_normalized": source_url_normalized,
            "business_id": diff_payload.get("business_id"),
            "crawl_run_id": diff_payload.get("crawl_run_id"),
            "monitor_event_id": diff_payload.get("monitor_event_id"),
            "diff_type": diff_payload.get("status") or "changed",
            "business_change_type": infer_business_change_type(offer),
            "affected_segment_ids": [],
            "before_text": "",
            "after_text": diff_payload.get("text_diff") or "",
            "before_hash": None,
            "after_hash": None,
            "proposed_action": proposed_action,
            "target_offer_id": matched_id if matched_id else None,
            "proposed_field_updates": {},
            "proposed_new_offer": {},
            "confidence": confidence,
            "confidence_label": confidence_label,
            "reason": reason,
            "validator_status": "pending",
            "validator_errors": [],
            "source_name": source_name,
        }

        if action == "update":
            event["proposed_field_updates"] = build_offer_update_payload(offer)
        elif action == "insert":
            event["proposed_new_offer"] = build_offer_insert_payload(
                offer,
                source_url=source_url,
                source_name=source_name,
            )
        elif action == "mark_ended":
            event["proposed_field_updates"] = {
                "is_active": False,
                "ended_reason": "explicit_successful_disappearance",
            }

        events.append(event)
        selected_candidate = _selected_match_candidate_payload(
            offer,
            candidate_offers,
            event_index=index,
        )
        if selected_candidate:
            match_candidates.append(selected_candidate)

    return {"change_events": events, "match_candidates": match_candidates}


_AUTO_APPLY_CHANGE_TYPES = {"price_changed", "eligibility_changed"}
_AUTO_APPLY_ACTIONS = {"update_offer"}
_AUTO_APPLY_UPDATE_FIELDS = set(_MASTER_TEXT_FIELDS + _MASTER_NUMERIC_FIELDS)


def validate_change_event_for_auto_apply(
    event: Dict[str, Any],
    *,
    min_confidence: float = 0.9,
) -> List[str]:
    """Return rule-gate errors that prevent automatic master-offer updates.

    This gate is deliberately conservative: only clear high-confidence updates
    to an already matched offer can auto-apply. New offers and missing-offer
    signals remain review items until identity and absence evidence are stronger.
    """
    errors: List[str] = []
    action = str(event.get("proposed_action") or "").strip()
    change_type = str(event.get("business_change_type") or "").strip()
    confidence = event.get("confidence")

    try:
        confidence_value = float(confidence)
    except (TypeError, ValueError):
        confidence_value = 0.0

    if confidence_value < min_confidence:
        errors.append("confidence_below_auto_apply_threshold")

    if action not in _AUTO_APPLY_ACTIONS:
        errors.append(f"action_requires_review:{action or 'unknown'}")

    if change_type not in _AUTO_APPLY_CHANGE_TYPES:
        errors.append(f"change_type_requires_review:{change_type or 'unknown'}")

    if action == "update_offer" and not event.get("target_offer_id"):
        errors.append("missing_target_offer_id")

    updates = event.get("proposed_field_updates") or {}
    if action == "update_offer":
        if not isinstance(updates, dict) or not updates:
            errors.append("empty_update_payload")
        elif any(field not in _AUTO_APPLY_UPDATE_FIELDS for field in updates):
            errors.append("unsupported_update_field")

    for field in _MASTER_NUMERIC_FIELDS:
        if isinstance(updates, dict) and field in updates:
            parsed = _parse_price(updates.get(field))
            if parsed is None or parsed < 0:
                errors.append(f"invalid_numeric_field:{field}")

    return errors


def build_change_event_decision_plan(
    payloads: Dict[str, Any],
    *,
    min_auto_apply_confidence: float = 0.9,
) -> Dict[str, Any]:
    """Apply rule validation to change events and split auto-apply vs review.

    The returned events keep the same DB shape as promo_offer_change_events, but
    validator_status is no longer pending: high-confidence safe updates become
    auto_apply; everything else is needs_review with machine-readable reasons.
    """
    events: List[Dict[str, Any]] = []
    auto_apply_events: List[Dict[str, Any]] = []
    review_events: List[Dict[str, Any]] = []

    for raw_event in payloads.get("change_events") or []:
        if not isinstance(raw_event, dict):
            continue
        event = dict(raw_event)
        existing_errors = event.get("validator_errors") or []
        if not isinstance(existing_errors, list):
            existing_errors = [str(existing_errors)]
        errors = [
            *existing_errors,
            *validate_change_event_for_auto_apply(
                event,
                min_confidence=min_auto_apply_confidence,
            ),
        ]

        if errors:
            event["validator_status"] = "needs_review"
            event["validator_errors"] = errors
            review_events.append(event)
        else:
            event["validator_status"] = "auto_apply"
            event["validator_errors"] = []
            auto_apply_events.append(event)
        events.append(event)

    return {
        "change_events": events,
        "match_candidates": payloads.get("match_candidates") or [],
        "auto_apply_events": auto_apply_events,
        "review_events": review_events,
        "decision_summary": {
            "events": len(events),
            "auto_apply": len(auto_apply_events),
            "needs_review": len(review_events),
            "min_auto_apply_confidence": min_auto_apply_confidence,
        },
    }


_CHANGE_EVENT_DB_FIELDS = {
    "change_event_id",
    "promo_website_id",
    "source_url",
    "source_url_normalized",
    "business_id",
    "crawl_run_id",
    "monitor_event_id",
    "diff_type",
    "business_change_type",
    "affected_segment_ids",
    "before_text",
    "after_text",
    "before_hash",
    "after_hash",
    "proposed_action",
    "target_offer_id",
    "proposed_field_updates",
    "proposed_new_offer",
    "confidence",
    "confidence_label",
    "reason",
    "validator_status",
    "validator_errors",
}
_MATCH_CANDIDATE_DB_FIELDS = {
    "change_event_id",
    "segment_id",
    "candidate_offer_id",
    "match_score",
    "match_method",
    "score_breakdown",
    "rank",
    "is_selected",
}


def _is_missing_rpc_error(exc: Exception, function: str) -> bool:
    message = f"{exc} {getattr(getattr(exc, 'response', None), 'text', '')}".lower()
    fn = function.lower()
    return fn in message and (
        "could not find the function" in message
        or "function not found" in message
        or "pgrst202" in message
        or "404" in message
    )


def _verify_change_driven_rpc(client: Any) -> None:
    """Ensure M019 atomic RPCs exist; refuse non-atomic REST writes if missing."""
    global _change_driven_rpc_verified
    if _change_driven_rpc_verified:
        return
    try:
        client.rpc(
            _RPC_PERSIST_CHANGE_EVENTS,
            {"p_events": [], "p_match_candidates": []},
        )
        probe = client.rpc(
            _RPC_APPLY_OFFER_ACTION,
            {"p_action": {"action": "__probe__"}},
        )
    except Exception as exc:
        if _is_missing_rpc_error(exc, _RPC_PERSIST_CHANGE_EVENTS) or _is_missing_rpc_error(
            exc, _RPC_APPLY_OFFER_ACTION
        ):
            raise RuntimeError(
                "M019 change-driven atomic RPCs are not deployed; "
                "apply config/sql/m019_atomic_change_driven_writes.sql before production writes"
            ) from exc
        raise
    if not isinstance(probe, dict) or probe.get("error") != "invalid_action":
        raise RuntimeError(
            f"Unexpected probe response from {_RPC_APPLY_OFFER_ACTION}: {probe!r}"
        )
    _change_driven_rpc_verified = True


def _offer_failure_summary(offer: Dict[str, Any]) -> str:
    action = str(offer.get("action") or "insert").strip().lower()
    if action in {"update", "mark_ended"}:
        target = str(offer.get("matched_id") or "").strip()
        if target:
            return f"{action}:{target}"
    service = str(offer.get("service_name") or "").strip()
    if service:
        return f"{action}:{service}"
    text = str(offer.get("offer_raw_text") or "").strip()
    if text:
        return f"{action}:{text[:80]}"
    return action or "unknown"


def _record_offer_failure(
    failed: List[Dict[str, Any]],
    *,
    offer: Dict[str, Any],
    error: Any,
) -> None:
    action = str(offer.get("action") or "insert").strip().lower()
    target_id = str(offer.get("matched_id") or "").strip() or None
    failed.append(
        {
            "action": action,
            "target_id": target_id,
            "target_summary": _offer_failure_summary(offer),
            "error": str(error),
        }
    )


def _build_rpc_offer_action_payload(
    offer: Dict[str, Any],
    *,
    offer_id: Any,
    master_payload: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    action = str(offer.get("action") or "insert").strip().lower()
    payload: Dict[str, Any] = {"action": action}
    if offer_id is not None:
        payload["offer_id"] = offer_id
    if master_payload is not None:
        payload["master"] = master_payload
    item = build_offer_item_payload(offer)
    payload["items"] = [item] if item.get("item_name") else []
    return payload


def _apply_offer_action_via_rpc(
    client: Any,
    offer: Dict[str, Any],
    *,
    offer_id: Any,
    master_payload: Optional[Dict[str, Any]],
    now_iso: str,
    failed: List[Dict[str, Any]],
) -> bool:
    rpc_payload = _build_rpc_offer_action_payload(
        offer,
        offer_id=offer_id,
        master_payload=master_payload,
    )
    try:
        result = client.rpc(
            _RPC_APPLY_OFFER_ACTION,
            {"p_action": rpc_payload, "p_now": now_iso},
        )
    except Exception as exc:
        _record_offer_failure(failed, offer=offer, error=exc)
        log.error(
            "change_driven: RPC apply failed for {summary}: {error}",
            summary=_offer_failure_summary(offer),
            error=exc,
        )
        return False
    if not isinstance(result, dict) or not result.get("ok"):
        error = (result or {}).get("error") if isinstance(result, dict) else result
        _record_offer_failure(failed, offer=offer, error=error or "rpc_apply_failed")
        log.error(
            "change_driven: RPC apply rejected {summary}: {error}",
            summary=_offer_failure_summary(offer),
            error=error,
        )
        return False
    return True


def prepare_change_event_insert_rows(
    payloads: Dict[str, Any],
) -> Dict[str, List[Dict[str, Any]]]:
    """Prepare DB-shaped rows for change events and candidate matches."""
    raw_events = payloads.get("change_events") or []
    raw_candidates = payloads.get("match_candidates") or []

    event_rows: List[Dict[str, Any]] = []
    event_id_by_index: Dict[int, str] = {}
    for fallback_index, event in enumerate(raw_events, start=1):
        if not isinstance(event, dict):
            continue
        event_index = event.get("event_index") or fallback_index
        try:
            event_index_int = int(event_index)
        except (TypeError, ValueError):
            event_index_int = fallback_index
        change_event_id = str(event.get("change_event_id") or uuid.uuid4())
        event_id_by_index[event_index_int] = change_event_id
        row = {
            key: value
            for key, value in event.items()
            if key in _CHANGE_EVENT_DB_FIELDS and value is not None
        }
        row["change_event_id"] = change_event_id
        event_rows.append(row)

    match_rows: List[Dict[str, Any]] = []
    for candidate in raw_candidates:
        if not isinstance(candidate, dict):
            continue
        event_index = candidate.get("event_index")
        try:
            event_index_int = int(event_index)
        except (TypeError, ValueError):
            event_index_int = 1 if len(event_id_by_index) == 1 else 0
        change_event_id = candidate.get("change_event_id") or event_id_by_index.get(event_index_int)
        if not change_event_id:
            continue
        row = {
            key: value
            for key, value in candidate.items()
            if key in _MATCH_CANDIDATE_DB_FIELDS and value is not None
        }
        row["change_event_id"] = str(change_event_id)
        match_rows.append(row)

    return {"change_event_rows": event_rows, "match_candidate_rows": match_rows}


def persist_change_event_payloads(
    client: Any,
    payloads: Dict[str, Any],
    *,
    dry_run: bool = True,
) -> Dict[str, Any]:
    """Persist prepared change-event payloads when explicitly enabled."""
    prepared = prepare_change_event_insert_rows(payloads)
    event_rows = prepared["change_event_rows"]
    match_rows = prepared["match_candidate_rows"]
    result: Dict[str, Any] = {
        **prepared,
        "change_events_inserted": 0,
        "match_candidates_inserted": 0,
        "dry_run": dry_run,
        "failed": [],
    }
    if dry_run:
        return result

    _verify_change_driven_rpc(client)
    try:
        rpc_result = client.rpc(
            _RPC_PERSIST_CHANGE_EVENTS,
            {
                "p_events": event_rows,
                "p_match_candidates": match_rows,
            },
        )
    except Exception as exc:
        result["failed"].append(
            {
                "action": "persist_change_events",
                "target_id": None,
                "target_summary": f"events={len(event_rows)},candidates={len(match_rows)}",
                "error": str(exc),
            }
        )
        log.error("change_driven: RPC persist change events failed: {error}", error=exc)
        return result

    if not isinstance(rpc_result, dict) or not rpc_result.get("ok"):
        error = (rpc_result or {}).get("error") if isinstance(rpc_result, dict) else rpc_result
        result["failed"].append(
            {
                "action": "persist_change_events",
                "target_id": None,
                "target_summary": f"events={len(event_rows)},candidates={len(match_rows)}",
                "error": str(error or "rpc_persist_failed"),
            }
        )
        return result

    result["change_events_inserted"] = int(rpc_result.get("change_events_inserted") or 0)
    result["match_candidates_inserted"] = int(
        rpc_result.get("match_candidates_inserted") or 0
    )
    return result


def sql_quote(value: Any) -> str:
    """Format a Python value as a Postgres SQL literal.

    Numbers -> bare numeric; None/empty string -> NULL; everything else ->
    single-quoted string with `'` escaped to `''`.
    """
    if value is None:
        return "NULL"
    if isinstance(value, bool):
        return "TRUE" if value else "FALSE"
    if isinstance(value, (int, float)):
        return str(value)
    text = str(value).strip()
    if text == "":
        return "NULL"
    return "'" + text.replace("'", "''") + "'"


def _sql_value_for_field(field: str, value: Any) -> str:
    if field in _MASTER_NUMERIC_FIELDS:
        parsed = _parse_price(value)
        return "NULL" if parsed is None else str(parsed)
    return sql_quote(value)


def build_offer_sql_statements(
    offers: List[Dict[str, Any]],
    *,
    source_url: str,
    source_name: str,
    now_iso: str,
) -> List[str]:
    """Render the same decisions apply_offer_actions writes via PostgREST as
    SQL text, for audit/traceability in the monitor report. Not executed."""
    statements: List[str] = []
    for offer in offers:
        action = str(offer.get("action") or "insert").strip().lower()

        if action == "insert":
            payload = build_offer_insert_payload(
                offer, source_url=source_url, source_name=source_name
            )
            service_name = str(offer.get("service_name") or "").strip()
            if not service_name and not payload.get("offer_raw_text"):
                continue
            cols = list(payload.keys())
            vals = [_sql_value_for_field(col, payload[col]) for col in cols]
            col_list = ", ".join(cols)
            val_list = ", ".join(vals)
            statements.append(
                f"INSERT INTO promo_offer_master ({col_list}) VALUES ({val_list});"
            )
            item = build_offer_item_payload(offer)
            if item.get("item_name"):
                statements.append(
                    "INSERT INTO promo_offer_items (offer_id, item_name, unit_type) "
                    f"VALUES (currval(pg_get_serial_sequence('promo_offer_master','id')), "
                    f"{sql_quote(item['item_name'])}, {sql_quote(item.get('unit_type'))});"
                )
            continue

        matched_id = str(offer.get("matched_id") or "").strip()

        if action == "mark_ended":
            if not matched_id:
                continue
            statements.append(
                "UPDATE promo_offer_master SET is_active=FALSE "
                f"WHERE id={sql_quote(matched_id)};"
            )
            continue

        if action == "update":
            if not matched_id:
                continue
            payload = build_offer_update_payload(offer)
            if not payload:
                continue
            set_parts = [
                f"{field}={_sql_value_for_field(field, payload[field])}"
                for field in payload
            ]
            set_clause = ", ".join(set_parts)
            statements.append(
                f"UPDATE promo_offer_master SET {set_clause} "
                f"WHERE id={sql_quote(matched_id)};"
            )
            continue
    return statements


def _resolve_promotion_id(
    client: Any,
    *,
    source_url: str,
    business_id: Any,
) -> Optional[int]:
    if business_id is None:
        return None
    try:
        bid = int(business_id)
    except (TypeError, ValueError):
        return None
    try:
        return upsert_promotion(client, business_id=bid, source_url=source_url)
    except Exception as exc:
        log.warning(
            "change_driven: promotion upsert failed for {url}: {error}",
            url=source_url,
            error=exc,
        )
        existing = fetch_promotion_by_url(client, source_url, business_id=bid)
        if existing:
            return int(existing["promotion_id"])
        return None


def _coerce_offer_id(offer_id: Any) -> Optional[int]:
    try:
        return int(offer_id)
    except (TypeError, ValueError):
        return None


# ---------------------------------------------------------------------------
# Apply actions to promo_offer_master
# ---------------------------------------------------------------------------

def apply_offer_actions(
    client: Any,
    offers: List[Dict[str, Any]],
    *,
    source_url: str,
    source_name: str,
    business_id: Any = None,
    dry_run: bool = False,
) -> Dict[str, Any]:
    """Apply validated change-driven actions into promo_offer_master."""
    if not offers:
        return {
            "updated": 0,
            "inserted": 0,
            "ended": 0,
            "skipped": 0,
            "failed": [],
            "sql_statements": [],
        }

    now_iso = datetime.now(timezone.utc).isoformat()
    updated = 0
    inserted = 0
    ended = 0
    skipped = 0
    failed: List[Dict[str, Any]] = []

    promotion_id = _resolve_promotion_id(
        client, source_url=source_url, business_id=business_id
    )

    if not dry_run:
        _verify_change_driven_rpc(client)

    for offer in offers:
        action = str(offer.get("action") or "insert").strip().lower()
        service_name = str(offer.get("service_name") or "").strip()

        if action == "update":
            matched_id = str(offer.get("matched_id") or "").strip()
            payload = build_offer_update_payload(offer)
            if not matched_id or not payload:
                skipped += 1
                continue
            if dry_run:
                updated += 1
                continue
            if _apply_offer_action_via_rpc(
                client,
                offer,
                offer_id=matched_id,
                master_payload=payload,
                now_iso=now_iso,
                failed=failed,
            ):
                updated += 1
            continue

        if action == "mark_ended":
            matched_id = str(offer.get("matched_id") or "").strip()
            if not matched_id:
                skipped += 1
                continue
            if dry_run:
                ended += 1
                continue
            if _apply_offer_action_via_rpc(
                client,
                offer,
                offer_id=matched_id,
                master_payload={"is_active": False},
                now_iso=now_iso,
                failed=failed,
            ):
                ended += 1
            continue

        payload = build_offer_insert_payload(
            offer,
            source_url=source_url,
            source_name=source_name,
            business_id=business_id,
            promotion_id=promotion_id,
        )
        if not service_name:
            skipped += 1
            continue
        if not payload.get("offer_raw_text"):
            skipped += 1
            continue
        if business_id is None:
            log.warning(
                "change_driven: skip offer without business_id for {url} service={service}",
                url=source_url,
                service=service_name,
            )
            skipped += 1
            continue
        check_payload = {**payload, "service_name": service_name}
        if should_exclude_from_offer_master(check_payload):
            log.warning(
                "change_driven: skip non-service offer for {url} service={service}",
                url=source_url,
                service=service_name,
            )
            skipped += 1
            continue
        existing_id = find_active_offer_by_fingerprint(
            client,
            business_id=business_id,
            offer_fingerprint=str(payload.get("offer_fingerprint") or ""),
        )
        if existing_id:
            if dry_run:
                updated += 1
                continue
            if _apply_offer_action_via_rpc(
                client,
                offer,
                offer_id=existing_id,
                master_payload=payload,
                now_iso=now_iso,
                failed=failed,
            ):
                updated += 1
            continue

        if dry_run:
            inserted += 1
            continue
        if _apply_offer_action_via_rpc(
            client,
            offer,
            offer_id=None,
            master_payload=payload,
            now_iso=now_iso,
            failed=failed,
        ):
            inserted += 1

    # SQL audit trail: render the same decisions as SQL text using the same
    # now_iso used for actual writes, so the audit statements match the writes.
    try:
        sql_statements = build_offer_sql_statements(
            offers,
            source_url=source_url,
            source_name=source_name,
            now_iso=now_iso,
        )
    except Exception as exc:
        sql_statements = []
        log.error("change_driven: failed to build SQL audit for {url}: {error}", url=source_url, error=exc)

    return {
        "updated": updated,
        "inserted": inserted,
        "ended": ended,
        "skipped": skipped,
        "failed": failed,
        "sql_statements": sql_statements,
    }


# ---------------------------------------------------------------------------
# Main pipeline: pages → LLM → promo_offer_master
# ---------------------------------------------------------------------------

def extract_and_upsert_check_pages(
    pages: List[Dict[str, Any]],
    client_llm: StructuredLLMClient,
    client_db: Any,
    domain_name: str,
    *,
    dry_run: bool = False,
    min_confidence: str = "low",
    include_change_events: bool = False,
    auto_apply_high_confidence: bool = True,
) -> Dict[str, Any]:
    """Full change-driven pipeline for one check's meaningful changed pages."""
    pages_with_diff = 0
    pages_without_diff = 0
    pages_with_write_failure = 0
    total_offers_extracted = 0
    total_updated = 0
    total_inserted = 0
    total_ended = 0
    total_auto_apply_events = 0
    total_review_events = 0
    candidates_unavailable = False
    page_results: List[Dict[str, Any]] = []

    for page in pages:
        url = (page.get("url") or "").strip()
        payload = extract_diff_payload(page)

        if payload is None:
            pages_without_diff += 1
            page_results.append({"url": url, "action": "no_diff_data"})
            log.info("change_driven: {url} -> no diff data, will need Apify fallback", url=url)
            continue

        if _CONF_RANK.get(payload.get("confidence"), 1) < _CONF_RANK.get(min_confidence, 1):
            pages_without_diff += 1
            page_results.append(
                {
                    "url": url,
                    "action": "low_confidence_skipped",
                    "confidence": payload.get("confidence"),
                }
            )
            log.info(
                "change_driven: {url} -> low confidence ({c}), skipping LLM",
                url=url,
                c=payload.get("confidence"),
            )
            continue

        pages_with_diff += 1
        page_candidates_unavailable = False
        try:
            candidate_offers = fetch_candidate_offers(client_db, url)
        except Exception as exc:
            candidate_offers = []
            candidates_unavailable = True
            page_candidates_unavailable = True
            log.warning(
                "change_driven: failed to fetch candidate offers for {url}: {error}",
                url=url,
                error=exc,
            )

        candidate_pool_size = len(candidate_offers)
        candidate_offers = filter_candidates_by_diff_relevance(
            candidate_offers, payload.get("meaningful_changes") or []
        )

        messages = build_change_extraction_messages(payload, domain_name, candidate_offers)

        try:
            raw_response = client_llm.create_json_response(
                messages,
                json_schema=build_change_extraction_json_schema(),
            )
        except Exception as exc:
            log.error(
                "change_driven: LLM call failed for {url}: {error}", url=url, error=exc
            )
            pages_without_diff += 1
            page_results.append({"url": url, "action": "llm_error", "error": str(exc)})
            continue

        if not isinstance(raw_response, dict) or "offers" not in raw_response:
            log.error(
                "change_driven: invalid JSON payload for {url}, will need Apify fallback",
                url=url,
            )
            pages_without_diff += 1
            page_results.append({"url": url, "action": "invalid_llm_payload"})
            continue

        validated = validate_offer_actions(
            raw_response,
            candidate_offers,
            source_url=url,
            candidates_unavailable=page_candidates_unavailable,
        )
        standardized_offers = standardize_offer_service_names(
            validated["offers"],
            candidate_offers,
        )
        extracted_offers = enrich_update_actions_with_diff_prices(
            standardized_offers,
            payload,
            candidate_offers,
        )
        total_offers_extracted += len(extracted_offers)

        if extracted_offers:
            change_payloads = build_change_event_payloads(
                extracted_offers,
                payload,
                candidate_offers,
                source_url=url,
                source_name=domain_name,
            )
            decision_plan = build_change_event_decision_plan(change_payloads)
            eligible_indexes = {
                int(event.get("event_index", 0)) - 1
                for event in decision_plan["auto_apply_events"]
                if str(event.get("event_index", "")).isdigit()
            }
            offers_to_apply = (
                [offer for index, offer in enumerate(extracted_offers) if index in eligible_indexes]
                if auto_apply_high_confidence
                else []
            )
            apply_result = apply_offer_actions(
                client_db,
                offers_to_apply,
                source_url=url,
                source_name=domain_name,
                business_id=payload.get("business_id"),
                dry_run=dry_run,
            )
            apply_result["proposed_offers"] = len(extracted_offers)
            apply_result["withheld_for_review"] = len(extracted_offers) - len(offers_to_apply)
            total_updated += apply_result["updated"]
            total_inserted += apply_result["inserted"]
            total_ended += apply_result["ended"]
            write_failed = bool(apply_result.get("failed"))
            page_result = {
                "url": url,
                "action": "extracted",
                "offers_extracted": len(extracted_offers),
                "ended": apply_result["ended"],
                "downgraded": validated["downgraded"],
                "candidates_unavailable": page_candidates_unavailable,
                "candidate_pool_size": candidate_pool_size,
                "candidate_kept": len(candidate_offers),
                **apply_result,
                **({"offer_actions": extracted_offers} if dry_run else {}),
            }
            total_auto_apply_events += len(decision_plan["auto_apply_events"])
            total_review_events += len(decision_plan["review_events"])
            if include_change_events:
                page_result.update(decision_plan)
                if not dry_run:
                    page_result["change_event_persistence"] = persist_change_event_payloads(
                        client_db, decision_plan, dry_run=False
                    )
                    persist_failed = page_result["change_event_persistence"].get("failed") or []
                    if persist_failed:
                        write_failed = True
                        page_result.setdefault("failed", []).extend(persist_failed)
            if write_failed:
                pages_with_write_failure += 1
                page_result["write_failed"] = True
                page_result["action"] = "write_failed"
            page_results.append(page_result)
            log.info(
                "change_driven: {url} -> {n} offers (updated={u}, inserted={i}, ended={e}, downgraded={d})",
                url=url,
                n=len(extracted_offers),
                u=apply_result["updated"],
                i=apply_result["inserted"],
                e=apply_result["ended"],
                d=validated["downgraded"],
            )
        else:
            page_result = {
                "url": url,
                "action": "extracted_empty",
                "offers_extracted": 0,
                "ended": 0,
                "downgraded": validated["downgraded"],
                "skipped": validated["skipped"],
                "candidates_unavailable": page_candidates_unavailable,
                "candidate_pool_size": candidate_pool_size,
                "candidate_kept": len(candidate_offers),
                **({"offer_actions": []} if dry_run else {}),
            }
            if include_change_events:
                page_result.update({"change_events": [], "match_candidates": []})
            page_results.append(page_result)
            log.info("change_driven: {url} -> 0 offers extracted from diff", url=url)

    return {
        "pages_with_diff": pages_with_diff,
        "pages_without_diff": pages_without_diff,
        "pages_with_write_failure": pages_with_write_failure,
        "total_offers_extracted": total_offers_extracted,
        "total_updated": total_updated,
        "total_inserted": total_inserted,
        "total_ended": total_ended,
        "total_auto_apply_events": total_auto_apply_events,
        "total_review_events": total_review_events,
        "needs_apify_fallback": pages_without_diff > 0 or pages_with_write_failure > 0,
        "candidates_unavailable": candidates_unavailable,
        "page_results": page_results,
    }
