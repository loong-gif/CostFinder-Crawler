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
from functools import lru_cache
from pathlib import Path
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

from utils.logger import log
from utils.offer_extraction_llm import (
    OFFER_OUTPUT_FIELDS,
    OpenAICompatibleClient,
    normalize_offer_record,
    parse_json_payload,
)

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

_MAX_TEXT_DIFF_CHARS = 3000
_MAX_CANDIDATE_OFFERS = 100
_MAX_CANDIDATE_TEXT_CHARS = 200
_CONF_RANK = {"low": 1, "medium": 2, "high": 3}
_VALID_ACTIONS = {"update", "insert", "mark_ended"}
_CANDIDATE_FETCH_VARIANTS = [
    (
        "id,service_name,offer_raw_text,regular_price,discount_price,original_price,status",
        "updated_at.desc",
    ),
    (
        "id,service_name,offer_raw_text,regular_price,discount_price,status",
        "created_at.desc",
    ),
    (
        "id,service_name,offer_raw_text,discount_price,status",
        "created_at.desc",
    ),
    (
        "id,service_name,offer_raw_text,status",
        "created_at.desc",
    ),
]

CHANGE_EXTRACTION_SYSTEM_PROMPT = (
    "You extract aesthetic service offers from website change data. "
    "You receive structured diff data showing what changed on a medspa or aesthetics website, "
    "plus existing active offers already stored for this exact page. "
    "Extract ONLY offers affected by this change. "
    "Return one of three actions per offer: update, insert, or mark_ended. "
    "Use update when the diff changes an existing stored offer and matched_candidate_index must point to one provided candidate. "
    "Use insert when the diff adds a brand-new offer that does not match any candidate. "
    "Use mark_ended when the diff shows an existing stored offer was removed or ended; matched_candidate_index is required and all other fields may be empty strings. "
    "Do not generate database ids. Only select from the provided candidate indexes. "
    "When pricing, dates, membership terms, or unit details are reasonably supported by the diff or candidate context, fill the structured fields instead of leaving them blank. "
    "If a pricing block contains multiple offers, split them into separate records. "
    "Return strict JSON with a single top-level key 'offers'."
)

# Fields from OFFER_OUTPUT_FIELDS that map directly to promo_offer_master columns.
# (offer_content and evidence_segments are internal and have no master column.)
_MASTER_TEXT_FIELDS = [
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
_MASTER_NUMERIC_FIELDS = [
    "regular_price",
    "discount_price",
    "discount_amount",
    "discount_percent",
    "membership_price",
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
_SERVICE_NAME_DICT_PATH = (
    Path(__file__).resolve().parents[1]
    / "CF_Extrator_Agent"
    / "data"
    / "service_name_dict.json"
)


# ---------------------------------------------------------------------------
# Diff payload extraction
# ---------------------------------------------------------------------------

def _head_tail(text: str, limit: int) -> str:
    if len(text) <= limit:
        return text
    half = limit // 2
    return text[:half] + "\n...[truncated middle]...\n" + text[-half:]


def extract_diff_payload(page: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    """Parse a Firecrawl check page into a compact diff payload.

    Returns None when no usable diff data is present, signalling that this
    page needs the Apify fallback.
    """
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

    judgment_reason = (judgment.get("reason") or "").strip()
    confidence = (judgment.get("confidence") or "").strip().lower()

    if not text_diff and not json_diff and not meaningful_changes:
        return None

    return {
        "url": (page.get("url") or "").strip(),
        "status": (page.get("status") or "").strip(),
        "text_diff": _head_tail(text_diff, _MAX_TEXT_DIFF_CHARS) if text_diff else "",
        "json_diff": json_diff,
        "meaningful_changes": meaningful_changes,
        "judgment_reason": judgment_reason,
        "confidence": confidence,
    }


# ---------------------------------------------------------------------------
# Candidate offer fetching / prompt construction
# ---------------------------------------------------------------------------

def _truncate_text(value: Any, limit: int) -> str:
    text = str(value or "").strip()
    if len(text) <= limit:
        return text
    return text[: limit - 3].rstrip() + "..."


@lru_cache(maxsize=1)
def _load_service_name_dictionary() -> Dict[str, Any]:
    try:
        return json.loads(_SERVICE_NAME_DICT_PATH.read_text(encoding="utf-8"))
    except (FileNotFoundError, json.JSONDecodeError):
        return {"standardized_names": [], "aliases": {}}


@lru_cache(maxsize=1)
def _get_standardized_service_names() -> List[str]:
    dictionary = _load_service_name_dictionary()
    names = [
        str(item).strip()
        for item in dictionary.get("standardized_names", [])
        if str(item).strip()
    ]
    return names or ["Others"]


def _normalize_service_name_from_dictionary(*candidates: Any) -> str:
    dictionary = _load_service_name_dictionary()
    standardized_names = set(_get_standardized_service_names())
    aliases = {
        str(key).strip().lower(): str(value).strip()
        for key, value in (dictionary.get("aliases") or {}).items()
        if str(key).strip() and str(value).strip()
    }

    for candidate in candidates:
        text = str(candidate or "").strip()
        if not text:
            continue
        lowered = text.lower()
        if text in standardized_names:
            return text
        if lowered in aliases:
            return aliases[lowered]
        for alias_key, standardized in aliases.items():
            if alias_key in lowered or lowered in alias_key:
                return standardized
    return "Others"


def fetch_candidate_offers(
    client: Any,
    source_url: str,
    *,
    limit: int = _MAX_CANDIDATE_OFFERS,
) -> List[Dict[str, Any]]:
    """Fetch active master offers for a page and compress them for LLM context."""
    last_error: Optional[Exception] = None
    rows: List[Dict[str, Any]] = []
    for select, order in _CANDIDATE_FETCH_VARIANTS:
        try:
            rows = client.fetch_rows(
                "promo_offer_master",
                select,
                filters={
                    "source_url": f"eq.{source_url}",
                    "status": "eq.active",
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
            "service_name": str(row.get("service_name") or "").strip(),
            "offer_raw_text": _truncate_text(
                row.get("offer_raw_text"), _MAX_CANDIDATE_TEXT_CHARS
            ),
            "regular_price": row.get("regular_price"),
            "discount_price": row.get("discount_price"),
            "original_price": row.get("original_price"),
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
    allowed_service_names = json.dumps(_get_standardized_service_names(), ensure_ascii=False)

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


def _offer_change_matches(
    previous: Any,
    current: Any,
    *,
    offer: Dict[str, Any],
    candidate: Dict[str, Any],
) -> bool:
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

    for side in (previous, current):
        if not isinstance(side, dict):
            continue
        side_name = _normalize_match_text(side.get("service_name"))
        side_text = _normalize_match_text(side.get("offer_raw_text"))

        if side_name and any(side_name == target or side_name in target or target in side_name for target in target_names):
            return True
        if side_text and any(side_text == target or side_text in target or target in side_text for target in target_texts):
            return True

    return False


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

    enriched: List[Dict[str, Any]] = []
    for offer in offers:
        if str(offer.get("action") or "").strip().lower() != "update":
            enriched.append(offer)
            continue

        matched_id = str(offer.get("matched_id") or "").strip()
        candidate = candidate_by_id.get(matched_id, {})
        if not candidate:
            enriched.append(offer)
            continue

        current_regular = _parse_price(offer.get("regular_price"))
        current_discount = _parse_price(offer.get("discount_price"))
        if current_regular is not None and current_discount is not None:
            enriched.append(offer)
            continue

        best_pair: Optional[Dict[str, Any]] = None
        for pair in changed_pairs:
            if _offer_change_matches(
                pair.get("previous"),
                pair.get("current"),
                offer=offer,
                candidate=candidate,
            ):
                best_pair = pair
                break

        if best_pair is None:
            enriched.append(offer)
            continue

        previous_prices = _extract_offer_price_fields(best_pair.get("previous"))
        current_prices = _extract_offer_price_fields(best_pair.get("current"))

        if current_discount is None:
            current_discount = (
                current_prices["discount_price"]
                or current_prices["regular_price"]
            )
            if current_discount is None:
                current_discount = (
                    _parse_price(candidate.get("discount_price"))
                    or _parse_price(candidate.get("regular_price"))
                    or _parse_price(candidate.get("original_price"))
                )

        if current_regular is None:
            current_regular = (
                previous_prices["regular_price"]
                or previous_prices["discount_price"]
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
        enriched.append(enriched_offer)

    return enriched


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

    standardized_offers: List[Dict[str, Any]] = []
    for offer in offers:
        action = str(offer.get("action") or "").strip().lower()
        if action == "mark_ended":
            standardized_offers.append(offer)
            continue

        matched_id = str(offer.get("matched_id") or "").strip()
        candidate = candidate_by_id.get(matched_id, {})
        standardized_offer = dict(offer)
        raw_service_name = str(
            offer.get("raw_service_name")
            or offer.get("service_name")
            or ""
        ).strip()
        standardized_offer["raw_service_name"] = raw_service_name
        standardized_offer["service_name"] = _normalize_service_name_from_dictionary(
            offer.get("service_name"),
            raw_service_name,
            offer.get("membership_name"),
            offer.get("offer_raw_text"),
            offer.get("offer_content"),
            candidate.get("service_name"),
            candidate.get("offer_raw_text"),
        )
        standardized_offers.append(standardized_offer)

    return standardized_offers


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

        offer = normalize_change_offer_record(raw_offer)
        action = offer["action"]
        matched_id = offer["matched_id"]
        matched_candidate_index = offer["matched_candidate_index"]

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
                    continue

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
                    continue

        if action == "insert":
            matched_id = ""
            matched_candidate_index = ""
            if not offer.get("service_name") and not offer.get("offer_raw_text"):
                skipped += 1
                continue

        if action == "mark_ended":
            mark_ended_offer = {field: "" for field in _CHANGE_EXTRACTION_FIELDS}
            mark_ended_offer["action"] = "mark_ended"
            mark_ended_offer["matched_id"] = matched_id
            mark_ended_offer["matched_candidate_index"] = matched_candidate_index
            validated.append(mark_ended_offer)
            continue

        offer["action"] = action
        offer["matched_id"] = matched_id
        offer["matched_candidate_index"] = matched_candidate_index
        validated.append(offer)

    return {"offers": validated, "downgraded": downgraded, "skipped": skipped}


def build_offer_update_payload(offer: Dict[str, Any]) -> Dict[str, Any]:
    payload: Dict[str, Any] = {}

    for field in _MASTER_TEXT_FIELDS:
        value = str(offer.get(field) or "").strip()
        if value:
            payload[field] = value

    for field in _MASTER_NUMERIC_FIELDS:
        value = _parse_price(offer.get(field))
        if value is not None:
            payload[field] = value

    return payload


def build_offer_insert_payload(
    offer: Dict[str, Any],
    *,
    source_url: str,
    source_name: str,
) -> Dict[str, Any]:
    payload = build_offer_update_payload(offer)
    payload.update(
        {
            "channel": "web_change_driven",
            "status": "active",
            "source_url": source_url,
            "source_name": source_name,
        }
    )
    return payload


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
            if not payload.get("service_name") and not payload.get("offer_raw_text"):
                continue
            cols = list(payload.keys())
            vals = [_sql_value_for_field(col, payload[col]) for col in cols]
            col_list = ", ".join(cols)
            val_list = ", ".join(vals)
            statements.append(
                f"INSERT INTO promo_offer_master ({col_list}) VALUES ({val_list});"
            )
            continue

        matched_id = str(offer.get("matched_id") or "").strip()

        if action == "mark_ended":
            if not matched_id:
                continue
            statements.append(
                "UPDATE promo_offer_master SET status='ended', "
                f"updated_at={sql_quote(now_iso)} WHERE id={sql_quote(matched_id)};"
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
            set_parts.append(f"updated_at={sql_quote(now_iso)}")
            set_clause = ", ".join(set_parts)
            statements.append(
                f"UPDATE promo_offer_master SET {set_clause} "
                f"WHERE id={sql_quote(matched_id)};"
            )
            continue
    return statements


def _update_master_row(
    client: Any,
    row_id: str,
    payload: Dict[str, Any],
    *,
    now_iso: str,
) -> None:
    payload_with_timestamp = {**payload, "updated_at": now_iso}
    try:
        client.update_row(
            "promo_offer_master",
            {"id": f"eq.{row_id}"},
            payload_with_timestamp,
        )
        return
    except Exception as exc:
        response_text = getattr(getattr(exc, "response", None), "text", "")
        error_text = f"{exc} {response_text}".strip()
        if "updated_at" not in error_text:
            raise
        if (
            "does not exist" not in error_text
            and "schema cache" not in error_text
            and "PGRST204" not in error_text
        ):
            raise
        client.update_row(
            "promo_offer_master",
            {"id": f"eq.{row_id}"},
            payload,
        )


# ---------------------------------------------------------------------------
# Apply actions to promo_offer_master
# ---------------------------------------------------------------------------

def apply_offer_actions(
    client: Any,
    offers: List[Dict[str, Any]],
    *,
    source_url: str,
    source_name: str,
    dry_run: bool = False,
) -> Dict[str, Any]:
    """Apply validated change-driven actions into promo_offer_master."""
    if not offers:
        return {"updated": 0, "inserted": 0, "ended": 0, "skipped": 0, "sql_statements": []}

    now_iso = datetime.now(timezone.utc).isoformat()
    updated = 0
    inserted = 0
    ended = 0
    skipped = 0

    for offer in offers:
        action = str(offer.get("action") or "insert").strip().lower()

        if action == "update":
            matched_id = str(offer.get("matched_id") or "").strip()
            payload = build_offer_update_payload(offer)
            if not matched_id or not payload:
                skipped += 1
                continue
            if dry_run:
                updated += 1
                continue
            try:
                _update_master_row(client, matched_id, payload, now_iso=now_iso)
                updated += 1
            except Exception as exc:
                log.error(
                    "Failed to update master offer id={id}: {error}",
                    id=matched_id,
                    error=exc,
                )
            continue

        if action == "mark_ended":
            matched_id = str(offer.get("matched_id") or "").strip()
            if not matched_id:
                skipped += 1
                continue
            if dry_run:
                ended += 1
                continue
            try:
                _update_master_row(
                    client,
                    matched_id,
                    {"status": "ended"},
                    now_iso=now_iso,
                )
                ended += 1
            except Exception as exc:
                log.error(
                    "Failed to end master offer id={id}: {error}",
                    id=matched_id,
                    error=exc,
                )
            continue

        payload = build_offer_insert_payload(
            offer,
            source_url=source_url,
            source_name=source_name,
        )
        if not payload.get("service_name") and not payload.get("offer_raw_text"):
            skipped += 1
            continue
        if dry_run:
            inserted += 1
            continue
        try:
            client.insert_rows("promo_offer_master", [payload])
            inserted += 1
        except Exception as exc:
            log.error(
                "Failed to insert master offer for {url}: {error}",
                url=source_url,
                error=exc,
            )

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
        "sql_statements": sql_statements,
    }


# ---------------------------------------------------------------------------
# Main pipeline: pages → LLM → promo_offer_master
# ---------------------------------------------------------------------------

def extract_and_upsert_check_pages(
    pages: List[Dict[str, Any]],
    client_llm: OpenAICompatibleClient,
    client_db: Any,
    domain_name: str,
    *,
    dry_run: bool = False,
    min_confidence: str = "low",
) -> Dict[str, Any]:
    """Full change-driven pipeline for one check's meaningful changed pages."""
    pages_with_diff = 0
    pages_without_diff = 0
    total_offers_extracted = 0
    total_updated = 0
    total_inserted = 0
    total_ended = 0
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
            raw_response = client_llm.create_json_response(messages)
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
            apply_result = apply_offer_actions(
                client_db,
                extracted_offers,
                source_url=url,
                source_name=domain_name,
                dry_run=dry_run,
            )
            total_updated += apply_result["updated"]
            total_inserted += apply_result["inserted"]
            total_ended += apply_result["ended"]
            page_results.append(
                {
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
            )
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
            page_results.append(
                {
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
            )
            log.info("change_driven: {url} -> 0 offers extracted from diff", url=url)

    return {
        "pages_with_diff": pages_with_diff,
        "pages_without_diff": pages_without_diff,
        "total_offers_extracted": total_offers_extracted,
        "total_updated": total_updated,
        "total_inserted": total_inserted,
        "total_ended": total_ended,
        "needs_apify_fallback": pages_without_diff > 0,
        "candidates_unavailable": candidates_unavailable,
        "page_results": page_results,
    }
