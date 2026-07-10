"""Membership plan extraction and persistence helpers."""
from __future__ import annotations

import re
from typing import Any, Dict, Iterable, List, Optional, Set, TYPE_CHECKING

if TYPE_CHECKING:
    from utils.offer_extraction_llm import OpenAICompatibleClient

_VALID_BILLING_PERIODS = {"monthly", "annual", "weekly"}

def build_membership_extraction_messages(row: Dict[str, Any], page_content: str) -> List[Dict[str, str]]:
    return [
        {
            "role": "system",
            "content": (
                "You extract aesthetic clinic membership plans from page text. "
                "Return strict JSON with a single top-level key membership_plans (array). "
                "Each plan must include tier_name, plan_name, monthly_fee, annual_fee, billing_period, "
                "benefits (non-priced perks), and priced_offers (services with member pricing). "
                "Do not invent values not supported by the text."
            ),
        },
        {
            "role": "user",
            "content": (
                f"Domain: {row.get('domain_name', '')}\n"
                f"Page: {row.get('subpage_url', '')}\n\n"
                "Output JSON shape:\n"
                '{"membership_plans":[{"tier_name":"TRU SIGNATURE","plan_name":"TRU SIGNATURE $199 MONTH",'
                '"monthly_fee":199.0,"annual_fee":null,"billing_period":"monthly",'
                '"benefits":[{"type":"free_treatment","desc":"1 free treatment/month"}],'
                '"priced_offers":[{"service_name":"botox","price":11.5,"unit_type":"unit",'
                '"regular_price":13.5,"members_only":true}]}]}\n\n'
                "Rules:\n"
                "- tier_name must be a human-readable tier (Platinum, VIP, Essentials).\n"
                "- If no tier name exists, use Standard or plan_name.\n"
                "- benefits: perks without standalone service SKU pricing.\n"
                "- priced_offers: concrete service + price rows for promo_offer_master.\n"
                "- billing_period: monthly | annual | weekly.\n\n"
                f"Text:\n{page_content}"
            ),
        },
    ]


def _parse_fee(value: Any) -> Optional[float]:
    if value in (None, ""):
        return None
    if isinstance(value, (int, float)):
        return float(value)
    match = re.search(r"\d+(?:\.\d+)?", str(value).replace(",", ""))
    if not match:
        return None
    try:
        return float(match.group(0))
    except ValueError:
        return None


def normalize_membership_plan(record: Dict[str, Any]) -> Dict[str, Any]:
    tier_name = str(record.get("tier_name") or record.get("plan_name") or "Standard").strip()
    plan_name = str(record.get("plan_name") or tier_name).strip()
    billing_period = str(record.get("billing_period") or "monthly").strip().lower()
    if billing_period not in _VALID_BILLING_PERIODS:
        billing_period = "monthly"

    benefits = record.get("benefits") or []
    if not isinstance(benefits, list):
        benefits = [benefits] if benefits else []

    priced_offers = record.get("priced_offers") or []
    if not isinstance(priced_offers, list):
        priced_offers = []

    return {
        "tier_name": tier_name,
        "plan_name": plan_name,
        "monthly_fee": _parse_fee(record.get("monthly_fee")),
        "annual_fee": _parse_fee(record.get("annual_fee")),
        "billing_period": billing_period,
        "benefits": benefits,
        "priced_offers": [item for item in priced_offers if isinstance(item, dict)],
    }


def normalize_membership_payload(payload: Any) -> List[Dict[str, Any]]:
    from utils.offer_extraction_llm import parse_json_payload

    data = parse_json_payload(payload, {})
    plans = data.get("membership_plans", []) if isinstance(data, dict) else []
    if not isinstance(plans, list):
        return []
    return [normalize_membership_plan(item) for item in plans if isinstance(item, dict)]


def build_membership_plan_insert_row(plan: Dict[str, Any], staging_row: Dict[str, Any]) -> Dict[str, Any]:
    tier_name = str(plan.get("tier_name") or plan.get("plan_name") or "Standard").strip()
    plan_name = str(plan.get("plan_name") or tier_name).strip()
    row: Dict[str, Any] = {
        "domain_name": str(staging_row.get("domain_name") or "").strip().lower(),
        "plan_name": plan_name,
        "billing_period": plan["billing_period"],
        "benefits": plan["benefits"],
        "source_url": str(staging_row.get("subpage_url") or staging_row.get("source_url") or "").strip(),
    }
    if staging_row.get("promo_website_id") is not None:
        row["promo_website_id"] = staging_row.get("promo_website_id")
    if staging_row.get("business_id") is not None:
        row["business_id"] = staging_row["business_id"]
    if plan.get("monthly_fee") is not None:
        row["monthly_fee"] = plan["monthly_fee"]
    if plan.get("annual_fee") is not None:
        row["annual_fee"] = plan["annual_fee"]
    return row


def build_priced_offer_insert_row(
    priced_offer: Dict[str, Any],
    *,
    membership_plan_id: int,
    staging_row: Dict[str, Any],
) -> Dict[str, Any]:
    from utils.offer_extraction_llm import canonicalize_service_name

    service_name = canonicalize_service_name(
        priced_offer.get("service_name"),
        priced_offer.get("offer_raw_text"),
    )
    price = _parse_fee(priced_offer.get("price") or priced_offer.get("discount_price"))
    regular_price = _parse_fee(priced_offer.get("regular_price") or priced_offer.get("original_price"))
    unit_type = str(priced_offer.get("unit_type") or "").strip()
    offer_raw_text = str(priced_offer.get("offer_raw_text") or "").strip()
    if not offer_raw_text:
        parts = [service_name]
        if price is not None:
            parts.append(f"${price:g}")
        if unit_type:
            parts.append(f"per {unit_type}")
        offer_raw_text = " ".join(parts)

    row: Dict[str, Any] = {
        "channel": "Website",
        "status": "active",
        "source_url": str(staging_row.get("subpage_url") or "").strip(),
        "source_name": str(staging_row.get("domain_name") or "").strip(),
        "service_name": service_name,
        "offer_raw_text": offer_raw_text,
        "membership_plan_id": membership_plan_id,
    }
    if staging_row.get("business_id") is not None:
        row["business_id"] = staging_row["business_id"]
    if price is not None:
        row["discount_price"] = price
    if regular_price is not None:
        row["regular_price"] = regular_price
    if unit_type:
        row["unit_type"] = unit_type
    if priced_offer.get("members_only"):
        row["is_membership_required"] = True
    return row


def membership_offer_fingerprint(
    *,
    membership_plan_id: int,
    service_name: str,
    unit_type: str,
    discount_price: Any,
) -> str:
    price = _parse_fee(discount_price)
    return "|".join(
        [
            str(membership_plan_id),
            service_name.lower(),
            unit_type.lower(),
            "" if price is None else f"{price:g}",
        ]
    )


def extract_membership_plans_for_row(
    row: Dict[str, Any],
    *,
    client: Optional["OpenAICompatibleClient"] = None,
    page_content: Optional[str] = None,
) -> List[Dict[str, Any]]:
    content = (page_content if page_content is not None else row.get("page_content") or "").strip()
    if not content:
        return []
    if client is None:
        return []
    messages = build_membership_extraction_messages(row, content)
    payload = client.create_json_response(messages)
    return normalize_membership_payload(payload)


def _is_pure_membership_offer(offer: Dict[str, Any]) -> bool:
    service_name = str(offer.get("service_name") or "").lower()
    offer_raw = str(offer.get("offer_raw_text") or "").lower()
    if "member" in service_name or "membership" in service_name:
        return True
    if re.search(r"/\s*mo\b|/month|monthly", offer_raw):
        if not re.search(r"\b(botox|filler|tox|unit|syringe)\b", offer_raw):
            return True
    return False


def find_stale_membership_offer_ids(
    client: Any,
    source_url: str,
    *,
    exclude_ids: Optional[Set[str]] = None,
) -> List[str]:
    """Find promo_offer_master rows that look like mis-filed membership plans."""
    exclude_ids = exclude_ids or set()
    filters = {"source_url": f"eq.{source_url}", "status": "eq.active"}
    rows = client.fetch_rows(
        "promo_offer_master",
        "id,service_name,offer_raw_text,membership_plan_id",
        filters=filters,
        limit=500,
    )
    stale: List[str] = []
    for row in rows:
        row_id = str(row.get("id") or "").strip()
        if not row_id or row_id in exclude_ids:
            continue
        if row.get("membership_plan_id"):
            continue
        if _is_pure_membership_offer(row):
            stale.append(row_id)
    return stale


def end_offer_ids(client: Any, offer_ids: Iterable[str], *, dry_run: bool = False) -> int:
    ended = 0
    for offer_id in offer_ids:
        if dry_run:
            ended += 1
            continue
        client.update_row(
            "promo_offer_master",
            {"id": f"eq.{offer_id}"},
            {"status": "ended"},
        )
        ended += 1
    return ended


def _normalize_source_url(url: Any) -> str:
    return str(url or "").strip().rstrip("/")


def infer_tier_name_from_offer(offer: Dict[str, Any]) -> str:
    raw = str(offer.get("raw_service_name") or "").strip()
    if raw and raw.lower() != "membership":
        return raw
    membership_name = str(offer.get("membership_name") or "").strip()
    if membership_name:
        return membership_name
    offer_content = offer.get("offer_content")
    content_text = ""
    if isinstance(offer_content, str):
        content_text = offer_content.strip()
    text = " ".join(
        part for part in (str(offer.get("offer_raw_text") or "").strip(), content_text) if part
    )
    cleaned = re.sub(
        r"\$\s*\d+(?:,\d{3})*(?:\.\d+)?(?:\s*/\s*(?:mo|month|yr|year|annual|week))?",
        "",
        text,
        flags=re.IGNORECASE,
    )
    cleaned = re.sub(
        r"\b(?:per\s+month|per\s+year|monthly|annual|yearly|weekly)\b",
        "",
        cleaned,
        flags=re.IGNORECASE,
    )
    cleaned = re.sub(r"\s+", " ", cleaned).strip(" -–|")
    if cleaned and cleaned.lower() not in {"membership", "member"}:
        return cleaned
    return "Standard"


def infer_billing_period_from_offer(offer: Dict[str, Any]) -> str:
    text = " ".join(
        str(offer.get(field) or "")
        for field in ("offer_raw_text", "billing_period", "offer_content")
        if not isinstance(offer.get(field), (dict, list))
    ).lower()
    if re.search(r"/\s*(?:yr|year|annual)\b|\b(?:annual|yearly)\b", text):
        return "annual"
    if re.search(r"/\s*week\b|\bweekly\b", text):
        return "weekly"
    return "monthly"


def infer_plan_fees_from_offer(
    offer: Dict[str, Any],
) -> tuple[Optional[float], Optional[float], str]:
    text = str(offer.get("offer_raw_text") or "")
    billing = infer_billing_period_from_offer(offer)
    monthly: Optional[float] = None
    annual: Optional[float] = None

    for pattern in (
        r"\$\s*(\d+(?:,\d{3})*(?:\.\d+)?)\s*/\s*(?:mo|month)\b",
        r"\$\s*(\d+(?:,\d{3})*(?:\.\d+)?)\s*(?:per\s+month|monthly)\b",
        r"(\d+(?:,\d{3})*(?:\.\d+)?)\s*/\s*month\b",
    ):
        match = re.search(pattern, text, re.IGNORECASE)
        if match:
            monthly = _parse_fee(match.group(1))
            break

    for pattern in (
        r"\$\s*(\d+(?:,\d{3})*(?:\.\d+)?)\s*/\s*(?:yr|year|annual)\b",
        r"\$\s*(\d+(?:,\d{3})*(?:\.\d+)?)\s*(?:per\s+year|annual)\b",
        r"(\d+(?:,\d{3})*(?:\.\d+)?)\s*/\s*year\b",
    ):
        match = re.search(pattern, text, re.IGNORECASE)
        if match:
            annual = _parse_fee(match.group(1))
            break

    if monthly is None and annual is None:
        fee = _parse_fee(offer.get("discount_price"))
        if fee is None:
            fee = _parse_fee(offer.get("membership_price"))
        if fee is not None:
            if billing == "annual":
                annual = fee
            else:
                monthly = fee

    return monthly, annual, billing


def offer_row_to_membership_plan(offer: Dict[str, Any]) -> Dict[str, Any]:
    tier_name = infer_tier_name_from_offer(offer)
    monthly_fee, annual_fee, billing_period = infer_plan_fees_from_offer(offer)
    if monthly_fee is not None:
        plan_name = f"{tier_name} ${monthly_fee:g}/month"
    elif annual_fee is not None:
        plan_name = f"{tier_name} ${annual_fee:g}/year"
    else:
        plan_name = tier_name
    return normalize_membership_plan(
        {
            "tier_name": tier_name,
            "plan_name": plan_name,
            "monthly_fee": monthly_fee,
            "annual_fee": annual_fee,
            "billing_period": billing_period,
            "benefits": [],
            "priced_offers": [],
        }
    )


def can_migrate_offer_to_plan(offer: Dict[str, Any]) -> bool:
    plan = offer_row_to_membership_plan(offer)
    return plan.get("monthly_fee") is not None or plan.get("annual_fee") is not None


def staging_context_from_offer(
    offer: Dict[str, Any],
    staging_row: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    source_url = str(offer.get("source_url") or "").strip()
    if staging_row:
        return {
            "domain_name": str(staging_row.get("domain_name") or offer.get("source_name") or "").strip().lower(),
            "subpage_url": str(staging_row.get("subpage_url") or source_url).strip(),
            "promo_website_id": staging_row.get("promo_website_id"),
            "business_id": staging_row.get("business_id") if staging_row.get("business_id") is not None else offer.get("business_id"),
            "crawl_timestamp": staging_row.get("crawl_timestamp"),
        }
    domain_name = str(offer.get("source_name") or "").strip().lower()
    if not domain_name and source_url:
        from urllib.parse import urlparse

        domain_name = urlparse(source_url).netloc.replace("www.", "").lower()
    return {
        "domain_name": domain_name,
        "subpage_url": source_url,
        "business_id": offer.get("business_id"),
    }


def build_membership_plan_insert_row_from_offer(
    offer: Dict[str, Any],
    staging_row: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    plan = offer_row_to_membership_plan(offer)
    ctx = staging_context_from_offer(offer, staging_row)
    return build_membership_plan_insert_row(plan, ctx)


def find_existing_plan_id(client: Any, source_url: str, tier_name: str) -> Optional[int]:
    from utils.membership_plan_lookup import normalize_plan_name

    norm_url = _normalize_source_url(source_url)
    tier_norm = normalize_plan_name(tier_name)
    if not norm_url or not tier_norm:
        return None
    for url in (norm_url, f"{norm_url}/"):
        try:
            rows = client.fetch_rows(
                "promo_membership_plans",
                "plan_id,plan_name,source_url",
                filters={"source_url": f"eq.{url}"},
                limit=100,
            )
        except Exception:
            continue
        for row in rows:
            if normalize_plan_name(row.get("plan_name")) == tier_norm:
                return int(row["plan_id"])
            if tier_norm in normalize_plan_name(row.get("plan_name")):
                return int(row["plan_id"])
    return None


def persist_membership_extraction(
    client: Any,
    staging_row: Dict[str, Any],
    plans: List[Dict[str, Any]],
    *,
    dry_run: bool = False,
) -> Dict[str, Any]:
    """Insert membership plans + priced offers; soft-end stale membership offers."""
    inserted_plans = 0
    inserted_offers = 0
    ended_offers = 0
    kept_offer_ids: Set[str] = set()
    plan_rows: List[Dict[str, Any]] = []

    for plan in plans:
        plan_row = build_membership_plan_insert_row(plan, staging_row)
        if dry_run:
            plan_id = -(inserted_plans + 1)
            inserted_plans += 1
        else:
            inserted = client.insert_rows("promo_membership_plans", [plan_row])
            if not inserted:
                continue
            plan_id = int(inserted[0]["plan_id"])
            inserted_plans += 1

        seen_fingerprints: Set[str] = set()
        for priced_offer in plan.get("priced_offers") or []:
            offer_row = build_priced_offer_insert_row(
                priced_offer,
                membership_plan_id=plan_id,
                staging_row=staging_row,
            )
            fingerprint = membership_offer_fingerprint(
                membership_plan_id=plan_id,
                service_name=str(offer_row.get("service_name") or ""),
                unit_type=str(offer_row.get("unit_type") or ""),
                discount_price=offer_row.get("discount_price"),
            )
            if fingerprint in seen_fingerprints:
                continue
            seen_fingerprints.add(fingerprint)

            if dry_run:
                inserted_offers += 1
                continue
            inserted_offer = client.insert_rows("promo_offer_master", [offer_row])
            if inserted_offer:
                kept_offer_ids.add(str(inserted_offer[0].get("id") or ""))
                inserted_offers += 1

        plan_rows.append({"plan_id": plan_id, **plan_row})

    source_url = str(staging_row.get("subpage_url") or "").strip()
    stale_ids = find_stale_membership_offer_ids(client, source_url, exclude_ids=kept_offer_ids)
    ended_offers = end_offer_ids(client, stale_ids, dry_run=dry_run)

    return {
        "plans_inserted": inserted_plans,
        "offers_inserted": inserted_offers,
        "offers_ended": ended_offers,
        "plans": plan_rows,
        "stale_offer_ids": stale_ids,
    }


def link_offers_to_membership_plans(
    offers: List[Dict[str, Any]],
    membership_plans: List[Dict[str, Any]],
) -> List[Dict[str, Any]]:
    """Attach membership_plan_index hints from priced_offer_indices when present."""
    linked = [dict(offer) for offer in offers]
    for plan_index, plan in enumerate(membership_plans):
        indices = plan.get("priced_offer_indices") or []
        if not isinstance(indices, list):
            continue
        for raw_index in indices:
            try:
                offer_index = int(raw_index)
            except (TypeError, ValueError):
                continue
            if 0 <= offer_index < len(linked):
                linked[offer_index]["membership_plan_index"] = plan_index
    return linked


def normalize_membership_plan_refs(
    membership_plans: List[Dict[str, Any]],
) -> List[Dict[str, Any]]:
    normalized: List[Dict[str, Any]] = []
    for plan in membership_plans:
        if not isinstance(plan, dict):
            continue
        item = normalize_membership_plan(plan)
        indices = plan.get("priced_offer_indices") or []
        if isinstance(indices, list):
            item["priced_offer_indices"] = [
                int(value) for value in indices if str(value).isdigit()
            ]
        normalized.append(item)
    return normalized


def normalize_offer_with_membership(
    record: Dict[str, Any],
    allowed_indexes: set[int],
) -> Dict[str, Any]:
    from utils.offer_extraction_llm import normalize_offer_record

    return normalize_offer_record(record, allowed_indexes)
