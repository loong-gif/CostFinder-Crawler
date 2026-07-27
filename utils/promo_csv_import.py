"""Pure planning helpers for importing curated promotion CSV rows."""
from __future__ import annotations

from dataclasses import dataclass
from datetime import date
import json
from typing import Any, Iterable, Mapping
from urllib.parse import urlparse

from utils.offer_field_normalize import normalize_bool
from utils.offer_fingerprint import compute_offer_fingerprint
from utils.offer_price_normalize import parse_price
from utils.offer_extraction_llm import canonicalize_service_name


def normalize_source_url(value: Any) -> str:
    return str(value or "").strip().rstrip("/")


def source_domain(value: Any) -> str:
    return urlparse(normalize_source_url(value)).netloc.casefold().removeprefix("www.")


def _normalized_text(value: Any) -> str:
    return " ".join(str(value or "").split()).casefold()


def _positive_price(value: Any) -> float | None:
    parsed = parse_price(value)
    return parsed if parsed is not None and parsed > 0 else None


def _valid_percent(value: Any) -> float | None:
    parsed = _positive_price(value)
    return parsed if parsed is not None and parsed <= 100 else None


def _positive_quantity(value: Any) -> float | None:
    return _positive_price(value)


def _is_per_unit(row: Mapping[str, Any]) -> bool:
    unit_type = _normalized_text(row.get("unit_type"))
    text = _normalized_text(row.get("offer_raw_text"))
    return unit_type in {"unit", "units"} or "/unit" in text or "per unit" in text


def _is_new_customer_required(row: Mapping[str, Any], raw_text: str) -> bool:
    text = f"{row.get('eligibility') or ''} {raw_text}".casefold()
    return any(phrase in text for phrase in ("new customer", "new patient", "new client", "first time"))


def _build_items(row: Mapping[str, Any], *, per_unit: bool, discount_price: float | None) -> list[dict[str, Any]]:
    content = str(row.get("offer_content") or "").strip()
    parsed: dict[str, Any] = {}
    if content:
        try:
            candidate = json.loads(content)
            if isinstance(candidate, dict):
                parsed = candidate
        except json.JSONDecodeError:
            pass
    items: list[dict[str, Any]] = []
    for label, value in parsed.items():
        name = canonicalize_service_name(label)
        if name == "Others":
            continue
        item: dict[str, Any] = {"service_name": name}
        quantity = _positive_quantity(value)
        if quantity is not None and not per_unit:
            item["quantity"] = int(quantity) if quantity.is_integer() else quantity
        items.append(item)
    if not items:
        item = {"service_name": canonicalize_service_name(row.get("service_name"), row.get("offer_raw_text"))}
        quantity = _positive_quantity(row.get("delivered_unit"))
        if quantity is not None and not per_unit:
            item["quantity"] = int(quantity) if quantity.is_integer() else quantity
        items.append(item)
    if per_unit and discount_price is not None and len(items) == 1:
        items[0]["unit_price"] = discount_price
    return items


@dataclass(frozen=True)
class PlannedOffer:
    source_row: int
    master: dict[str, Any]
    items: list[dict[str, Any]]
    needs_promotion: bool


@dataclass(frozen=True)
class RejectedOffer:
    source_row: int
    reason: str
    source_url: str
    offer_raw_text: str


@dataclass(frozen=True)
class ImportPlan:
    accepted: list[PlannedOffer]
    rejected: list[RejectedOffer]


def build_import_plan(
    rows: Iterable[Mapping[str, Any]],
    *,
    promotions_by_url: Mapping[str, Mapping[str, Any]],
    businesses_by_domain: Mapping[str, list[Mapping[str, Any]]],
    businesses_by_name: Mapping[str, list[Mapping[str, Any]]] | None = None,
    today: str | date | None = None,
) -> ImportPlan:
    """Map validated CSV rows into the live master/items table contract.

    Rows are only planned when their business mapping is unambiguous and they
    contain a concrete positive discount price, amount, or percentage.
    """
    as_of = date.fromisoformat(today) if isinstance(today, str) else today or date.today()
    accepted: list[PlannedOffer] = []
    rejected: list[RejectedOffer] = []
    seen: set[tuple[str, str]] = set()

    for source_row, source in enumerate(rows, start=2):
        row = dict(source)
        url = normalize_source_url(row.get("source_url"))
        raw_text = " ".join(str(row.get("offer_raw_text") or "").split())
        key = (url.casefold(), raw_text.casefold())
        if not url or not raw_text:
            rejected.append(RejectedOffer(source_row, "missing_source_or_evidence", url, raw_text))
            continue
        if key in seen:
            rejected.append(RejectedOffer(source_row, "duplicate_source_row", url, raw_text))
            continue
        seen.add(key)

        promotion = promotions_by_url.get(url)
        business_id = promotion.get("business_id") if promotion else None
        promotion_id = promotion.get("promotion_id") if promotion else None
        needs_promotion = False
        if business_id is None:
            candidates = businesses_by_domain.get(source_domain(url), [])
            if len(candidates) != 1 and businesses_by_name is not None:
                candidates = businesses_by_name.get(_normalized_text(row.get("source_name")), [])
            if len(candidates) != 1:
                rejected.append(RejectedOffer(source_row, "unmapped_business", url, raw_text))
                continue
            business_id = candidates[0].get("business_id")
            needs_promotion = True
        if not business_id:
            rejected.append(RejectedOffer(source_row, "unmapped_business", url, raw_text))
            continue

        regular_price = _positive_price(row.get("original_price"))
        discount_price = _positive_price(row.get("discount_price"))
        discount_amount = _positive_price(row.get("discount_amount"))
        discount_percent = _valid_percent(row.get("discount_percent"))
        if discount_price is None and discount_amount is None and discount_percent is None:
            rejected.append(RejectedOffer(source_row, "missing_discount_signal", url, raw_text))
            continue

        per_unit = _is_per_unit(row)
        quantity = _positive_quantity(row.get("delivered_unit"))
        end_date = str(row.get("end_date") or "").strip()
        is_active = True
        if end_date:
            try:
                is_active = date.fromisoformat(end_date) >= as_of
            except ValueError:
                rejected.append(RejectedOffer(source_row, "invalid_end_date", url, raw_text))
                continue

        service_name = canonicalize_service_name(row.get("service_name"), raw_text)
        unit_type = str(row.get("unit_type") or "").strip()
        master: dict[str, Any] = {
            "business_id": int(business_id),
            "is_active": is_active,
            "is_new_customer_required": _is_new_customer_required(row, raw_text),
            "is_membership_required": bool(normalize_bool(row.get("is_membership_required"))),
            "offer_raw_text": raw_text[:4000],
            "price_model": "from" if "starting at" in raw_text.casefold() else "per_unit" if per_unit else "total",
        }
        if promotion_id is not None:
            master["promotion_id"] = int(promotion_id)
        if regular_price is not None:
            master["regular_price"] = regular_price
        if discount_price is not None:
            master["discount_price"] = discount_price
        if discount_amount is not None:
            master["discount_amount"] = discount_amount
        if discount_percent is not None:
            master["discount_percent"] = discount_percent
        master["offer_fingerprint"] = compute_offer_fingerprint(
            source_url=url,
            service_name=service_name,
            unit_type=unit_type,
            regular_price=regular_price,
            discount_price=discount_price,
            offer_raw_text=raw_text,
        )
        accepted.append(PlannedOffer(source_row, master, _build_items(row, per_unit=per_unit, discount_price=discount_price), needs_promotion))

    return ImportPlan(accepted=accepted, rejected=rejected)
