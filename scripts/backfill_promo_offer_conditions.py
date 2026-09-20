#!/usr/bin/env python3
"""Plan and apply source-backed conditions without an external model.

The planner matches each Offer to its stored source and extracts only explicit
restrictions with small, conservative rules. Ambiguous matches stay in review.
"""
from __future__ import annotations

import argparse
from collections import defaultdict
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import re
import sys
from typing import Any

from dotenv import load_dotenv

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from utils.offer_evidence_validator import sanitize_conditions
from utils.supabase_rest import SupabaseRestClient, get_supabase_secret_key, get_supabase_writer_key


PAGE_SIZE = 1000
PLAN_VERSION = 1
OFFER_SELECT = (
    "id,business_id,promotion_id,membership_plan_id,offer_raw_text,regular_price,"
    "discount_price,discount_percent,discount_amount,is_membership_required,"
    "is_new_customer_required,updated_at,promo_offer_items(item_name,service_id,quantity:delivered_units,unit_price)"
)
PROMOTION_SELECT = "promotion_id,business_id,source_url,promotion_content,updated_at"
MEMBERSHIP_SELECT = (
    "plan_id,business_id,source_url,membership_name,minimum_commitment_months,benefits,updated_at"
)


def _fetch_all(
    client: SupabaseRestClient,
    table: str,
    select: str,
    *,
    filters: dict[str, str] | None = None,
) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    offset = 0
    while True:
        page = client.fetch_rows(
            table,
            select,
            filters=filters,
            limit=PAGE_SIZE,
            offset=offset,
            order="id.asc" if table == "promo_offer_master" else None,
        )
        rows.extend(page)
        if len(page) < PAGE_SIZE:
            return rows
        offset += PAGE_SIZE


def _in_filter(values: set[int]) -> str:
    return f"in.({','.join(str(value) for value in sorted(values))})"


def _json_text(value: Any) -> str:
    if isinstance(value, str):
        return value.strip()
    if value in (None, [], {}):
        return ""
    if isinstance(value, list):
        return "\n".join(part for item in value if (part := _json_text(item)))
    return json.dumps(value, ensure_ascii=False)


def _source_text(source_type: str, source: dict[str, Any]) -> str:
    if source_type == "promotion":
        return _json_text(source.get("promotion_content"))
    benefits = source.get("benefits")
    if isinstance(benefits, list):
        parts = [_json_text(item) for item in benefits if _json_text(item)]
    else:
        parts = [_json_text(benefits)] if _json_text(benefits) else []
    commitment = source.get("minimum_commitment_months")
    if commitment is not None:
        parts.append(f"Minimum commitment months: {commitment}")
    return "\n".join(parts)


def _source_key(offer: dict[str, Any]) -> tuple[str, int] | None:
    promotion_id = offer.get("promotion_id")
    membership_plan_id = offer.get("membership_plan_id")
    if promotion_id is not None and membership_plan_id is None:
        return "promotion", int(promotion_id)
    if membership_plan_id is not None and promotion_id is None:
        return "membership", int(membership_plan_id)
    return None


def _base_row(
    offer: dict[str, Any],
    *,
    source_type: str | None,
    source: dict[str, Any] | None,
    status: str = "review",
    reason: str | None = None,
    conditions: list[str] | None = None,
) -> dict[str, Any]:
    source_id = None
    source_url = None
    source_updated_at = None
    if source:
        source_id = source.get("promotion_id") if source_type == "promotion" else source.get("plan_id")
        source_url = source.get("source_url")
        source_updated_at = source.get("updated_at")
    return {
        "offer_id": int(offer["id"]),
        "business_id": offer.get("business_id"),
        "source_type": source_type,
        "source_id": source_id,
        "source_url": source_url,
        "source_updated_at": source_updated_at,
        "offer_updated_at": offer.get("updated_at"),
        "offer_raw_text": str(offer.get("offer_raw_text") or ""),
        "conditions": conditions,
        "status": status,
        "reason": reason,
    }


_STOP_WORDS = {
    "a", "an", "and", "at", "for", "from", "get", "in", "of", "on", "or", "per",
    "again", "buy", "customer", "discount", "free", "minimum", "member", "members",
    "non-member", "off", "only", "patient", "patients", "price", "purchase", "regular",
    "required", "save", "special", "syringe", "treatment", "the", "to", "unit", "units",
    "vial", "vials", "with", "your",
}
_NUMBER = r"\d+(?:\.\d+)?"
_UNIT = r"units?|u(?:nit)?s?|syringes?|vials?|sessions?|treatments?|days?|weeks?|months?|years?"


def _source_segments(source_text: str) -> list[str]:
    text = source_text.replace("<br>", "\n").replace("<br/>", "\n")
    lines = [
        re.sub(r"\s+", " ", line).strip(" -*\"'")
        for line in re.split(r"\n+|(?<=[.!?])\s+|[→•|]+", text)
    ]
    return [line for line in lines if line]


def _offer_terms(offer: dict[str, Any]) -> tuple[list[str], list[str]]:
    raw = str(offer.get("offer_raw_text") or "").casefold()
    items = offer.get("promo_offer_items") or []
    if isinstance(items, dict):
        items = [items]
    names = [str(item.get("item_name") or "").casefold() for item in items if isinstance(item, dict)]
    terms = set(names)
    terms.update(
        token
        for token in re.findall(r"[a-z][a-z0-9-]{2,}", raw)
        if token not in _STOP_WORDS
    )
    numbers = re.findall(_NUMBER, raw.replace(",", ""))
    return sorted(terms), numbers


def _number_hit_count(text: str, numbers: list[str]) -> int:
    normalized = re.sub(r"\s+", " ", text).casefold()
    return sum(
        1
        for number in dict.fromkeys(numbers)
        if re.search(rf"(?<![\d,]){re.escape(number)}(?![\d,])", normalized)
    )


def _match_source_segments(offer: dict[str, Any], source_text: str) -> list[str]:
    raw = str(offer.get("offer_raw_text") or "")
    normalized_raw = re.sub(r"\s+", " ", raw).casefold().strip()
    segments = _source_segments(source_text)
    exact = [segment for segment in segments if normalized_raw and normalized_raw in re.sub(r"\s+", " ", segment).casefold()]
    if exact:
        return exact[:2] if len(exact) <= 2 else []
    terms, numbers = _offer_terms(offer)
    anchor_terms = [term for term in terms if len(term) >= 4 and term not in _STOP_WORDS]
    scored: list[tuple[int, int, str]] = []
    scores_by_index: dict[int, tuple[int, int, int]] = {}
    for index, segment in enumerate(segments):
        normalized = re.sub(r"\s+", " ", segment).casefold()
        term_hits = sum(1 for term in anchor_terms if term and term in normalized)
        number_hits = _number_hit_count(segment, numbers)
        score = (3 * term_hits) + (2 * number_hits)
        if term_hits and score >= 3:
            scored.append((score, index, segment))
            scores_by_index[index] = (score, term_hits, number_hits)
    if not scored:
        return []
    best = max(score for score, _index, _segment in scored)
    best_indices = [index for score, index, _segment in scored if score == best]
    if len(best_indices) > 2:
        return []
    selected = set(best_indices)
    condition_signal = re.compile(
        r"minimum|at\s+least|after|within|every|anniversary|renewal|birthday|"
        r"pre[- ]?book|consult|coupon|combined|supplies\s+last|limited|"
        r"purchase|buy|free|complimentary|used\s+within",
        re.IGNORECASE,
    )
    # Pull in only an immediately adjacent clause that shares an offer anchor
    # or one of the offer's numeric values. This captures split source rows
    # without broadcasting a neighboring offer's restriction.
    for index in best_indices:
        for neighbor in (index - 1, index + 1):
            if neighbor < 0 or neighbor >= len(segments):
                continue
            segment = segments[neighbor]
            normalized = re.sub(r"\s+", " ", segment).casefold()
            term_hits = sum(1 for term in anchor_terms if term and term in normalized)
            number_hits = _number_hit_count(segment, numbers)
            if condition_signal.search(segment) and (term_hits or number_hits):
                selected.add(neighbor)
    return [segments[index] for index in sorted(selected)]


def _condition_candidates(segment: str) -> list[dict[str, str]]:
    text = re.sub(r"\s+", " ", segment).strip(" -*\"'")
    lowered = text.casefold()
    candidates: list[dict[str, str]] = []

    def add(requirement: str) -> None:
        requirement = re.sub(r"\s+", " ", requirement).strip(" -*\"'")
        if requirement:
            candidates.append({"requirement": requirement, "evidence": text})

    # New/first-time eligibility is stored in the boolean column. Do not copy
    # it into conditions, but keep other restrictions in the same sentence.
    if re.search(r"\breturn(?:ing)?\s+(?:patients?|clients?|customers?)\b|patients?\s+who\s+have\s+had", lowered):
        add("Returning customer required")
    minimum = re.search(
        rf"\b(?:minimum|at\s+least)\s+({_NUMBER})\s+({_UNIT})\b|\b({_NUMBER})\s+({_UNIT})\s+(?:minimum|required|or\s+more|\+)",
        text,
        re.IGNORECASE,
    )
    if minimum:
        number = minimum.group(1) or minimum.group(3)
        unit = minimum.group(2) or minimum.group(4)
        add(f"Minimum {number} {unit} required")
    for match in re.finditer(
        rf"\b(?:after|within|every|per)\s+({_NUMBER})\s+({_UNIT})\b[^.!?\n]*",
        text,
        re.IGNORECASE,
    ):
        add(match.group(0))
    if re.search(r"\b(?:anniversary|renewal|birthday)\b", lowered):
        add("Available on the stated anniversary or renewal")
    prebook = re.search(r"\bpre[- ]?book\b[^.!?\n]*", text, re.IGNORECASE)
    if prebook:
        add(prebook.group(0))
    consultation = re.search(r"\b(?:consultation|consult)\b[^.!?\n]{0,30}\b(?:required|must)\b", text, re.IGNORECASE)
    if consultation:
        add(consultation.group(0))
    if "coupon" in lowered and re.search(r"activate|redeem|manufacturer", lowered):
        add("Manufacturer coupon activation or redemption required")
    if re.search(r"\b(?:cannot|can't|can not|not)\s+be\s+combined\b|\bnot\s+combinable\b", lowered):
        add("Cannot be combined with other offers")
    if re.search(r"while\s+supplies\s+last|limited\s+(?:time|quantity|availability)", lowered):
        add("Limited availability")
    purchase = re.search(r"\b(?:with|upon)\s+purchase\s+of\b[^.!?\n]*|\bbuy\s+\d+\s+[^.!?\n]*", text, re.IGNORECASE)
    if purchase and re.search(r"free|complimentary|discount|save|only", lowered):
        add(f"Purchase requirement: {purchase.group(0)}")
    used = re.search(r"\bmust\s+be\s+used\s+within\s+\d+\s+(?:days?|weeks?|months?|years?)\b", text, re.IGNORECASE)
    if used:
        add(used.group(0))
    return candidates


def _extract_local_conditions(offer: dict[str, Any], source_text: str) -> tuple[list[str] | None, str | None]:
    matched = _match_source_segments(offer, source_text)
    if not matched:
        return None, "offer_not_located"
    segments = list(matched)
    conditions: list[dict[str, str]] = []
    for segment in segments:
        conditions.extend(_condition_candidates(segment))
    sanitized, issues = sanitize_conditions(conditions, source_text)
    if issues:
        return None, "invalid_conditions_evidence"
    raw = str(offer.get("offer_raw_text") or "").casefold()
    if re.search(r"\bminimum\b|\bat\s+least\b|\bafter\b|\bwithin\b|\bevery\b|"
                 r"\banniversary\b|\brenewal\b|\bpre[- ]?book\b|\bcoupon\b|"
                 r"\b(?:cannot|can't)\s+be\s+combined\b|\bnot\s+combinable\b|"
                 r"\b(?:with|upon)\s+purchase\b", raw):
        if not sanitized:
            return None, "condition_not_located"
        if "minimum" in raw and not any("minimum" in item.casefold() for item in sanitized):
            return None, "condition_not_located"
    return sanitized or [], None


def _resolve_group(
    offers: list[dict[str, Any]],
    *,
    source_type: str,
    source: dict[str, Any],
) -> list[dict[str, Any]]:
    source_text = _source_text(source_type, source)
    if not source_text:
        return [
            _base_row(offer, source_type=source_type, source=source, reason="source_content_missing")
            for offer in offers
        ]
    output: list[dict[str, Any]] = []
    for offer in offers:
        conditions, reason = _extract_local_conditions(offer, source_text)
        if reason:
            output.append(_base_row(offer, source_type=source_type, source=source, reason=reason))
        else:
            output.append(
                _base_row(
                    offer,
                    source_type=source_type,
                    source=source,
                    status="ready",
                    conditions=conditions or [],
                )
            )
    return output


def build_plan(client: SupabaseRestClient) -> dict[str, Any]:
    offers = _fetch_all(
        client,
        "promo_offer_master",
        OFFER_SELECT,
        filters={"is_active": "eq.true", "conditions": "is.null"},
    )
    promotion_ids = {int(offer["promotion_id"]) for offer in offers if offer.get("promotion_id") is not None}
    membership_ids = {
        int(offer["membership_plan_id"])
        for offer in offers
        if offer.get("membership_plan_id") is not None
    }
    promotions = (
        {
            int(row["promotion_id"]): row
            for row in _fetch_all(
                client,
                "clinic_promotions",
                PROMOTION_SELECT,
                filters={"promotion_id": _in_filter(promotion_ids)},
            )
        }
        if promotion_ids
        else {}
    )
    memberships = (
        {
            int(row["plan_id"]): row
            for row in _fetch_all(
                client,
                "clinic_memberships",
                MEMBERSHIP_SELECT,
                filters={"plan_id": _in_filter(membership_ids)},
            )
        }
        if membership_ids
        else {}
    )
    grouped: dict[tuple[str, int], list[dict[str, Any]]] = defaultdict(list)
    rows: list[dict[str, Any]] = []
    for offer in sorted(offers, key=lambda row: int(row["id"])):
        key = _source_key(offer)
        if key is None:
            rows.append(_base_row(offer, source_type=None, source=None, reason="no_condition_source"))
            continue
        source = promotions.get(key[1]) if key[0] == "promotion" else memberships.get(key[1])
        if source is None:
            source_stub = {"promotion_id": key[1]} if key[0] == "promotion" else {"plan_id": key[1]}
            rows.append(
                _base_row(
                    offer,
                    source_type=key[0],
                    source=source_stub,
                    reason="source_not_found",
                )
            )
            continue
        grouped[key].append(offer)

    for (source_type, source_id), source_offers in sorted(grouped.items()):
        source = promotions[source_id] if source_type == "promotion" else memberships[source_id]
        rows.extend(_resolve_group(source_offers, source_type=source_type, source=source))
    rows.sort(key=lambda row: int(row["offer_id"]))
    return {
        "plan_version": PLAN_VERSION,
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "scope": "active_conditions_null",
        "summary": {
            "targets": len(rows),
            "ready": sum(row["status"] == "ready" for row in rows),
            "review": sum(row["status"] == "review" for row in rows),
            "source_groups": len(grouped),
        },
        "rows": rows,
    }


def _current_sources(
    client: SupabaseRestClient,
    rows: list[dict[str, Any]],
) -> dict[tuple[str, int], dict[str, Any]]:
    refs = {(row.get("source_type"), row.get("source_id")) for row in rows}
    promotion_ids = {int(source_id) for source_type, source_id in refs if source_type == "promotion" and source_id is not None}
    membership_ids = {int(source_id) for source_type, source_id in refs if source_type == "membership" and source_id is not None}
    current: dict[tuple[str, int], dict[str, Any]] = {}
    if promotion_ids:
        for row in _fetch_all(
            client,
            "clinic_promotions",
            PROMOTION_SELECT,
            filters={"promotion_id": _in_filter(promotion_ids)},
        ):
            current[("promotion", int(row["promotion_id"]))] = row
    if membership_ids:
        for row in _fetch_all(
            client,
            "clinic_memberships",
            MEMBERSHIP_SELECT,
            filters={"plan_id": _in_filter(membership_ids)},
        ):
            current[("membership", int(row["plan_id"]))] = row
    return current


def apply_plan(
    plan: dict[str, Any],
    reader: SupabaseRestClient,
    writer: SupabaseRestClient,
) -> dict[str, Any]:
    if plan.get("plan_version") != PLAN_VERSION or plan.get("scope") != "active_conditions_null":
        raise ValueError("unsupported conditions backfill plan")
    ready = [row for row in plan.get("rows", []) if row.get("status") == "ready"]
    current_sources = _current_sources(reader, ready)
    offer_ids = {int(row["offer_id"]) for row in ready}
    current_offers = (
        {
            int(row["id"]): row
            for row in _fetch_all(
                reader,
                "promo_offer_master",
                "id,is_active,conditions,updated_at,promotion_id,membership_plan_id",
                filters={"id": _in_filter(offer_ids)},
            )
        }
        if offer_ids
        else {}
    )
    result: dict[str, Any] = {"applied": [], "conflicts": [], "review_rows": len(plan.get("rows", [])) - len(ready)}
    for row in sorted(ready, key=lambda item: int(item["offer_id"])):
        offer_id = int(row["offer_id"])
        source_key = (row.get("source_type"), row.get("source_id"))
        source = current_sources.get(source_key)
        if source is None or source.get("updated_at") != row.get("source_updated_at"):
            result["conflicts"].append({"offer_id": offer_id, "reason": "source_changed_since_plan"})
            continue
        current = current_offers.get(offer_id)
        if (
            current is None
            or current.get("is_active") is not True
            or current.get("conditions") is not None
            or current.get("updated_at") != row.get("offer_updated_at")
        ):
            result["conflicts"].append({"offer_id": offer_id, "reason": "offer_changed_since_plan"})
            continue
        updated = writer.update_row(
            "promo_offer_master",
            {
                "id": f"eq.{offer_id}",
                "is_active": "eq.true",
                "conditions": "is.null",
                "updated_at": f"eq.{row['offer_updated_at']}",
            },
            {"conditions": row["conditions"]},
        )
        if not updated:
            result["conflicts"].append({"offer_id": offer_id, "reason": "offer_changed_since_plan"})
            continue
        if updated[0].get("conditions") != row["conditions"]:
            result["conflicts"].append({"offer_id": offer_id, "reason": "write_verification_failed"})
            continue
        result["applied"].append(offer_id)
    all_ids = {int(row["offer_id"]) for row in plan.get("rows", [])}
    after_rows = (
        _fetch_all(
            reader,
            "promo_offer_master",
            "id,is_active,conditions,updated_at",
            filters={"id": _in_filter(all_ids)},
        )
        if all_ids
        else []
    )
    after_by_id = {int(row["id"]): row for row in after_rows}
    applied_set = set(result["applied"])
    ready_mismatches: list[dict[str, Any]] = []
    review_mismatches: list[dict[str, Any]] = []
    for row in plan.get("rows", []):
        current = after_by_id.get(int(row["offer_id"]))
        if current is None:
            (ready_mismatches if row.get("status") == "ready" else review_mismatches).append(
                {"offer_id": int(row["offer_id"]), "reason": "offer_missing_on_readback"}
            )
            continue
        if row.get("status") == "ready" and int(row["offer_id"]) in applied_set:
            if current.get("conditions") != row.get("conditions"):
                ready_mismatches.append(
                    {"offer_id": int(row["offer_id"]), "reason": "ready_conditions_mismatch"}
                )
        elif row.get("status") == "review" and current.get("conditions") is not None:
            review_mismatches.append(
                {"offer_id": int(row["offer_id"]), "reason": "review_conditions_changed"}
            )
    result["readback"] = {
        "targets": len(all_ids),
        "ready_mismatches": ready_mismatches,
        "review_mismatches": review_mismatches,
    }
    return result


def _load_reader() -> SupabaseRestClient:
    url = (os.getenv("SUPABASE_URL") or "").strip()
    key = os.getenv("SUPABASE_PUBLISHABLE_KEY") or os.getenv("SUPABASE_ANON_KEY") or get_supabase_secret_key()
    if not url or not key:
        raise RuntimeError("Missing SUPABASE_URL or a Supabase read credential")
    return SupabaseRestClient(url, key)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--apply", type=Path, help="Apply an existing plan JSON")
    parser.add_argument("--output", type=Path, help="Output JSON path")
    args = parser.parse_args()
    load_dotenv(PROJECT_ROOT / ".env")
    if args.apply:
        plan = json.loads(args.apply.read_text(encoding="utf-8"))
        reader = _load_reader()
        writer = SupabaseRestClient(os.environ["SUPABASE_URL"], get_supabase_writer_key())
        result = apply_plan(plan, reader, writer)
        output = args.output or args.apply.parent / (
            f"promo_offer_conditions_apply_{datetime.now(timezone.utc).strftime('%Y%m%d_%H%M%S')}.json"
        )
    else:
        plan = build_plan(_load_reader())
        result = plan
        output = args.output or PROJECT_ROOT / "output" / "results" / (
            f"promo_offer_conditions_plan_{datetime.now(timezone.utc).strftime('%Y%m%d_%H%M%S')}.json"
        )
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps({"output": str(output), **result.get("summary", result)}, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
