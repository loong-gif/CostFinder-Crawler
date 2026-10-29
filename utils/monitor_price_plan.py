"""Pure, source-scoped planning for promotion price synchronization.

No writes or clocks: callers supply the source graph and current page evidence.
Negative IDs are references local to each table, resolved by the locked writer.
``review`` plans never contain operations. Ready plans can contain warnings;
warnings inhibit removal and keep ``is_offer_extracted`` false.
"""
from __future__ import annotations

from copy import deepcopy
from datetime import date
from decimal import Decimal, InvalidOperation, ROUND_HALF_UP
import re
import unicodedata
from urllib.parse import urlparse
from typing import Any

from utils.offer_evidence_validator import (
    _NEW_CUSTOMER_SIGNAL, stated_paid_and_delivered, validate_offer_evidence,
)
from utils.offer_fingerprint import normalize_url
from utils.service_price_guard import infer_explicit_per_unit_price
from utils.monitor_price_writer import WriterError, normalize_source_url
from utils.offer_extraction_llm import canonicalize_service_name
from utils.promo_offer_items_db import is_blanket_offer_text
from utils.offer_purchase_semantics import new_customer_tier_merge_patch, service_supported_by_source


PROMOTIONS = "clinic_promotions"
OFFERS = "promo_offer_master"
ITEMS = "promo_offer_items"
TERMS = "promo_offer_terms"
_PRICE_FIELDS = ("regular_price", "discount_price", "discount_percent", "discount_amount")
_MONEY = re.compile(r"\$\s*\d[\d,]*(?:\.\d+)?|\b\d+(?:\.\d+)?\s*%")


class _Review(ValueError):
    pass


def _text(value: Any) -> str:
    value = unicodedata.normalize("NFKC", str(value or "")).casefold()
    return re.sub(r"[^\w]+", " ", value).strip()


def _raw_identity(value: Any) -> str:
    return _text(_MONEY.sub(" ", str(value or "")))


def _service_name_key(value: Any) -> str:
    canonical = canonicalize_service_name(value)
    return _text(canonical) if canonical and _text(canonical) != "others" else _text(value)


def _number(value: Any, field: str, *, positive: bool = False) -> Decimal | None:
    if value is None:
        return None
    if isinstance(value, bool):
        raise _Review(f"invalid_number:{field}")
    try:
        number = Decimal(str(value))
    except (InvalidOperation, ValueError):
        raise _Review(f"invalid_number:{field}") from None
    if not number.is_finite() or number < 0 or (positive and number == 0):
        raise _Review(f"invalid_number:{field}")
    return number


def _rounded(value: Decimal, places: str = "0.01") -> float:
    return float(value.quantize(Decimal(places), rounding=ROUND_HALF_UP))


def _id(value: Any) -> int:
    if not isinstance(value, int) or isinstance(value, bool) or value <= 0:
        raise _Review("invalid_row_id")
    return value


def _date(value: Any) -> str | None:
    if value is None or value == "":
        return None
    if not isinstance(value, str):
        raise _Review("invalid_campaign_date")
    try:
        return date.fromisoformat(value).isoformat()
    except ValueError:
        raise _Review("invalid_campaign_date") from None


def _campaign(offer: dict, evidence: dict) -> dict:
    fields = {
        "promotion_title": offer.get("campaign_title", evidence.get("promotion_title")),
        "campaign_start_date": offer.get("campaign_start_date", evidence.get("promotion_start_date", evidence.get("campaign_start_date"))),
        "campaign_end_date": offer.get("campaign_end_date", evidence.get("promotion_end_date", evidence.get("campaign_end_date"))),
    }
    fields["promotion_title"] = str(fields["promotion_title"] or "").strip() or None
    for key in ("campaign_start_date", "campaign_end_date"):
        if isinstance(fields[key], str) and not re.search(r"\d{4}", fields[key]):
            # Yearless model output (e.g. "October 31" or "10/31") is
            # unknown, not an invalid-date reason to quarantine the offer.
            short = re.fullmatch(r"(\d{1,2})/(\d{1,2})/(\d{2})", fields[key].strip())
            fields[key] = (f"20{short[3]}-{int(short[1]):02d}-{int(short[2]):02d}" if short else None)
        fields[key] = _date(fields[key])
        value = fields[key]
        if value:
            parsed = date.fromisoformat(value)
            month = rf"(?:{parsed.month}|{parsed.month:02d})"
            day = rf"(?:{parsed.day}|{parsed.day:02d})"
            month_name = parsed.strftime("%B")
            named = rf"(?:{month_name}|{month_name[:3]}\.?)"
            patterns = [re.escape(value), rf"{month}[-/]{day}[-/]{parsed.year}",
                        rf"{named}\s*{day}(?:st|nd|rd|th)?\s*,?\s+{parsed.year}",
                        rf"{day}(?:st|nd|rd|th)?\s+{named}\s*,?\s+{parsed.year}"]
            if 2000 <= parsed.year <= 2099:
                patterns.append(rf"{month}[-/]{day}[-/]{parsed.year % 100:02d}")
            text = str(evidence.get("date_text", evidence.get("markdown")) or "")
            if not any(re.search(rf"(?<!\w){pattern}(?!\w)", text, re.I) for pattern in patterns):
                fields[key] = None
    if fields["campaign_start_date"] and fields["campaign_end_date"] and fields["campaign_start_date"] > fields["campaign_end_date"]:
        raise _Review("invalid_campaign_date_range")
    return fields


def _campaign_key(row: dict) -> tuple:
    return (_text(row.get("promotion_title")), row.get("campaign_start_date"), row.get("campaign_end_date"))


_UNIT_IN_TEXT = re.compile(r"\b(\d+)\s*(units?|sessions?|syringes?|treatments?)\b", re.I)
_NON_MEMBER = re.compile(r"\bnon[- ]?members?\b", re.I)


def _variant_reconciliation_records(evidence: dict) -> list[dict]:
    recon = evidence.get("product_variant_reconciliation") or []
    if recon:
        return recon
    if evidence.get("variant_expected") is not None:
        return [{"variant_expected": evidence.get("variant_expected", 0),
                 "variant_skipped": evidence.get("variant_skipped") or [],
                 "variant_capture_count": len(evidence.get("variant_captures") or [])}]
    return []


def _variant_catalog_complete(evidence: dict) -> bool:
    records = _variant_reconciliation_records(evidence)
    if records:
        return all((rec.get("variant_capture_count") or 0) + len(rec.get("variant_skipped") or [])
                   == (rec.get("variant_expected") or 0) for rec in records if rec.get("variant_expected"))
    if evidence.get("variant_expected"):
        return (len(evidence.get("variant_captures") or []) + len(evidence.get("variant_skipped") or [])
                == evidence["variant_expected"])
    return evidence.get("variant_capture_complete") is True


def _check_variant_reconciliation(evidence: dict, raw_offers: list, warning) -> None:
    for rec in _variant_reconciliation_records(evidence):
        expected = int(rec.get("variant_expected") or 0)
        if expected <= 0:
            continue
        skipped = rec.get("variant_skipped") or []
        captured = int(rec.get("variant_capture_count") or 0)
        if captured + len(skipped) != expected:
            warning("variant_reconciliation_mismatch", None)
    purchasable = sum(int(rec.get("variant_capture_count") or 0) for rec in _variant_reconciliation_records(evidence))
    if not purchasable:
        purchasable = len(evidence.get("variant_captures") or [])
    variant_offers = [offer for offer in raw_offers if offer.get("purchase_variant")]
    if purchasable and len(variant_offers) != purchasable:
        warning("variant_reconciliation_mismatch", None)
    for index, raw in enumerate(raw_offers):
        if not raw.get("purchase_variant"):
            continue
        blob = " ".join([str(raw.get("offer_raw_text") or ""), *(
            c if isinstance(c, str) else str(c.get("requirement", "")) for c in (raw.get("conditions") or []))])
        if not _UNIT_IN_TEXT.search(blob):
            continue
        for item in raw.get("items") or []:
            delivered = item.get("delivered_units", item.get("quantity"))
            if delivered is None or delivered == 1:
                warning("variant_reconciliation_mismatch", index)


def _promotion_content(existing: dict | None, quotes: list[str], complete: bool) -> list:
    if complete or existing is None:
        return quotes
    # Partial extraction cannot erase the unprocessed part of the source.
    content = deepcopy(existing.get("promotion_content") or [])
    for quote in quotes:
        matches = [index for index, old in enumerate(content)
                   if isinstance(old, str) and _raw_identity(old) == _raw_identity(quote)]
        if len(matches) == 1:
            content[matches[0]] = quote
        elif quote not in content:
            content.append(quote)
    return content


def _resolve_items(offer: dict, services: list[dict], business_id: int) -> list[dict]:
    raw_items = offer.get("items")
    if not isinstance(raw_items, list) or not raw_items or any(not isinstance(i, dict) for i in raw_items):
        raise _Review("missing_or_invalid_items")
    blanket = is_blanket_offer_text(offer.get("offer_raw_text"))
    items = []
    for item in raw_items:
        candidate_name = item.get("service_name")
        invalid_hint = bool(candidate_name and _text(candidate_name) != "others" and not service_supported_by_source(candidate_name, offer.get("source_offer_raw_text", offer.get("offer_raw_text"))))
        if invalid_hint:
            candidate_name = None
        if not candidate_name or _text(candidate_name) == "others":
            candidate_name = item.get("item_name")
        name = str(candidate_name or "").strip()
        if not name:
            raise _Review("missing_item_name")
        area = _text(item.get("service_area"))
        if blanket:
            service_id, stored_name = None, str(item.get("item_name") or name).strip()
        else:
            service_id = item.get("service_id")
            candidates = [s for s in services if s.get("business_id") == business_id]
            if service_id is not None:
                _id(service_id)
                candidates = [s for s in candidates if s.get("service_id") == service_id]
                candidates = [s for s in candidates if _service_name_key(s.get("service_name")) == _service_name_key(name)]
            else:
                candidates = [s for s in candidates if _service_name_key(s.get("service_name")) == _service_name_key(name)]
            candidates = [s for s in candidates if not invalid_hint and service_supported_by_source(s.get("service_name"), offer.get("source_offer_raw_text", offer.get("offer_raw_text")))]
            if area:
                candidates = [s for s in candidates if _text(s.get("service_area")) == area]
            if len(candidates) == 1:
                service_id = candidates[0]["service_id"]
            else:
                # Store the evidenced item without guessing a catalogue FK.
                service_id = None
            # item_name keeps the source title; the catalogue name is only the FK's label.
            stored_name = str(item.get("item_name") or name).strip()
        quantity = _number(item.get("delivered_units", item.get("quantity")), "delivered_units", positive=True)
        _number(item.get("unit_price"), "unit_price")
        if offer.get("quantity_kind", "fixed") != "fixed":
            quantity = None
        if quantity is not None and (quantity != quantity.to_integral_value() or quantity > 32767):
            raise _Review("unrepresentable_delivered_quantity")
        items.append({"item_name": stored_name, "service_id": service_id,
                      "delivered_units": int(quantity) if quantity is not None else None,
                      "unit_price": None})
    keys = [i["service_id"] if i["service_id"] is not None else _text(i["item_name"]) for i in items]
    if len(keys) != len(set(keys)):
        raise _Review("duplicate_offer_service")
    return items


def _validated_offer(raw: dict, markdown: str, items: list[dict]) -> tuple[dict | None, str | None]:
    candidate = deepcopy(raw)
    for field in _PRICE_FIELDS:
        value = _number(candidate.get(field), field)
        if field == "discount_percent" and value is not None and value > 100:
            raise _Review("invalid_discount_percent")
    conditions = candidate.get("conditions")
    if isinstance(conditions, list):
        candidate["conditions"] = [
            {"requirement": c, "evidence": c} if isinstance(c, str) else c for c in conditions
        ]
    candidate["items"] = []
    for original, resolved in zip(raw["items"], items):
        item = deepcopy(original)
        item["quantity"] = resolved["delivered_units"]
        # Explicit rates still need validation when a percent/amount-only
        # per-unit offer legitimately has no master discount_price.
        if raw.get("price_model") != "per_unit" or raw.get("discount_price") is not None:
            item.pop("unit_price", None)
        candidate["items"].append(item)
    if (raw.get("discount_price") is not None and not raw.get("is_package") and len(items) == 1
            and infer_explicit_per_unit_price(str(raw.get("offer_raw_text") or "")) is None
            and infer_explicit_per_unit_price(str(raw.get("offer_raw_text") or ""), prefer_first=True) is not None):
        return None, "ambiguous_unit_price_context"
    decision = validate_offer_evidence(candidate, markdown)
    if not decision.accepted:
        return None, decision.reason
    if any(issue.field in _PRICE_FIELDS for issue in decision.issues):
        return None, "unsupported_price_evidence"
    # Unsupported quantity/unit/area fields are written as NULL (the validator already cleared them). Still
    # quarantine an unevidenced item rate (price evidence) and a new-customer flag the page itself mentions.
    unsafe = [issue for issue in decision.issues if issue.field not in _PRICE_FIELDS and not issue.field.endswith(".service_name")]
    if any(issue.field.endswith(".unit_price") for issue in unsafe):
        return None, "unsupported_price_evidence"
    if any(issue.field == "is_new_customer_required" for issue in unsafe) and _NEW_CUSTOMER_SIGNAL.search(markdown):
        return None, "unsupported_scope_or_quantity"
    for issue in unsafe:
        cleared = re.fullmatch(r"items\[(\d+)\]\.quantity", issue.field)
        if cleared and int(cleared.group(1)) < len(items):
            items[int(cleared.group(1))]["delivered_units"] = None
    offer = decision.offer
    model = raw.get("price_model", "total")
    quantity_kind = raw.get("quantity_kind", "fixed")
    if model not in {"total", "per_unit", "from"} or quantity_kind not in {"fixed", "minimum", "variable"}:
        raise _Review("unsupported_price_or_quantity_model")
    package = raw.get("is_package", len(items) > 1)
    for flag in ("is_package", "is_membership_required", "is_new_customer_required"):
        if flag in raw and not isinstance(raw[flag], bool):
            raise _Review(f"invalid_boolean:{flag}")
    if len(items) > 1 and not package:
        raise _Review("multiple_items_require_package")
    qty = _number(items[0]["delivered_units"], "delivered_units") if len(items) == 1 else None
    if model == "per_unit" and len(items) > 1:
        raise _Review("ambiguous_package_unit_price")
    prices = {field: offer.get(field) for field in _PRICE_FIELDS}
    rate_text = str(raw.get("offer_raw_text") or "")
    explicit_rate = infer_explicit_per_unit_price(rate_text)
    if (prices["discount_price"] is not None and not package and len(items) == 1 and explicit_rate is None
            and infer_explicit_per_unit_price(rate_text, prefer_first=True) is not None):
        return None, "ambiguous_unit_price_context"
    if model == "per_unit" and prices["discount_price"] is not None:
        if explicit_rate is None or abs(Decimal(str(explicit_rate)) - Decimal(str(prices["discount_price"]))) > Decimal(".0001"):
            return None, "per_unit_price_without_stated_rate"
    if model == "total" and prices["discount_price"] is not None and explicit_rate is not None:
        if abs(Decimal(str(explicit_rate))-Decimal(str(prices["discount_price"]))) <= Decimal(".0001"):
            model = "per_unit"
    if model == "from" and prices["discount_price"] is None and prices["regular_price"] is None:
        # Percent/amount-only discounts have no starting price to represent.
        model = "total"
    if model == "per_unit" and qty is not None:
        free_counts = stated_paid_and_delivered(str(raw.get("offer_raw_text") or ""))
        if free_counts and qty != free_counts[1]:
            return None, "paid_and_delivered_quantity_conflict"
        paid_qty = free_counts[0] if free_counts else qty
        for field in ("discount_price", "regular_price"):
            value = _number(prices[field], field)
            if value is not None:
                prices[field] = _rounded(value * (paid_qty if field == "discount_price" else qty))
        if prices["discount_amount"] is not None:
            prices["discount_amount"] = _rounded(_number(prices["discount_amount"], "discount_amount") * qty)
    regular = _number(prices["regular_price"], "regular_price")
    discount = _number(prices["discount_price"], "discount_price")
    if regular is not None and discount is not None and discount > regular:
        return None, "discount_exceeds_regular_price"
    if regular is not None and discount is not None:
        amount = _number(prices["discount_amount"], "discount_amount")
        percent = _number(prices["discount_percent"], "discount_percent")
        if (amount is not None and abs(regular-discount-amount) > Decimal(".011")
                or percent is not None and regular > 0 and
                abs((regular-discount)*100/regular-percent) >= Decimal("1")):
            return None, "contradictory_discount_evidence"
    bonus = stated_paid_and_delivered(rate_text)
    evidenced_bonus = bonus is not None and len(items) == 1 and qty == bonus[1]
    if not evidenced_bonus and not any(_number(prices[k], k) is not None and _number(prices[k], k) > 0 for k in _PRICE_FIELDS[1:]):
        return None, "no_supported_discount_field"
    for item_index, item in enumerate(items):
        delivered = _number(item["delivered_units"], "delivered_units")
        if not package and discount is not None:
            if delivered is not None:
                item["unit_price"] = _rounded(discount / delivered, "0.0001")
            else:
                item["unit_price"] = float(discount)
        elif not package and model == "per_unit":
            validated = offer["items"][item_index].get("unit_price")
            item["unit_price"] = float(_number(validated, "unit_price")) if validated is not None else None
    master = {**prices, "offer_raw_text": str(offer["offer_raw_text"]).strip(),
              "price_model": model, "is_package": package, "is_active": True,
              "is_membership_required": bool(offer.get("is_membership_required")),
              "is_new_customer_required": bool(offer.get("is_new_customer_required")),
              "conditions": offer["conditions"]}
    return master, None


def _item_key(item: dict, blanket: bool = False) -> tuple:
    return (None if blanket else item.get("service_id"),
            "" if blanket else (_text(item.get("item_name")) if item.get("service_id") is None else ""))


def _identity(master: dict, items: list[dict]) -> tuple:
    blanket = is_blanket_offer_text(master.get("offer_raw_text"))
    composition = tuple(sorted((_item_key(i, blanket) for i in items), key=repr))
    conditions = master.get("conditions")
    return (composition, _raw_identity(master.get("offer_raw_text")) if blanket else None,
            bool(master.get("is_membership_required")),
            bool(master.get("is_new_customer_required")),
            tuple(sorted({_text(c) for c in conditions or []})))


def _offer_match_key(master: dict, items: list[dict]) -> tuple:
    """Same service/composition; new-patient vs returning is one offer."""
    blanket = is_blanket_offer_text(master.get("offer_raw_text"))
    composition = tuple(sorted((_item_key(i, blanket) for i in items), key=repr))
    return (composition, _raw_identity(master.get("offer_raw_text")) if blanket else None,
            bool(master.get("is_membership_required")))


def _catalog_items(items: list[dict], services: list[dict]) -> list[dict]:
    names = {s["service_id"]: s["service_name"] for s in services}
    return [{**i, "item_name": names.get(i.get("service_id"), i.get("item_name"))} for i in items]


def _nullable_item_match(old: dict, old_items: list[dict], new: dict, new_items: list[dict]) -> bool:
    """Fallback only for absent mappings, preserving unique existing IDs."""
    if len(old_items) != len(new_items) or not any(i.get("service_id") is None for i in [*old_items, *new_items]):
        return False
    if bool(old.get("is_membership_required")) != bool(new.get("is_membership_required")):
        return False
    return sorted(_service_name_key(i.get("item_name")) for i in old_items) == sorted(
        _service_name_key(i.get("item_name")) for i in new_items)


def _term_values(master: dict) -> dict:
    conditions = master.get("conditions") or []
    if not isinstance(conditions, list) or any(not isinstance(c, str) for c in conditions):
        raise _Review("invalid_offer_conditions")
    text = " ".join([str(master.get("offer_raw_text") or ""), *conditions])
    unit_pattern = r"(units?|vials?|syringes?|sessions?|treatments?|areas?|items?)"
    minimum = re.search(r"\b(?:minimum|min\.?|at\s+least)\s+(\d+(?:\.\d+)?)\s*" + unit_pattern + r"\b", text, re.I)
    if minimum is None:
        minimum = re.search(r"\b(\d+(?:\.\d+)?)\s*(?:\+|or\s+more)\s*" + unit_pattern + r"\b", text, re.I)
    if minimum is None:
        minimum = re.search(r"\b(\d+(?:\.\d+)?)\s*" + unit_pattern + r"\s*(?:minimum|or\s+more)\b", text, re.I)
    min_quantity = None
    min_unit = None
    if minimum:
        min_quantity = float(Decimal(minimum.group(1)))
        unit = minimum.group(2).lower().rstrip("s")
        min_unit = {"unit":"unit", "vial":"vial", "syringe":"syringe",
                    "session":"session", "treatment":"treatment", "area":"area", "item":"item"}.get(unit)
        if min_unit is None:
            raise _Review("unsupported_minimum_purchase_unit")
    one_per = bool(re.search(r"\b(?:one|1)\s+per\s+(?:patient|person|client)\b|"
                             r"\b(?:once|one time|1 time)\s+per\s+(?:patient|person|client)\b", text, re.I))
    return {"raw_terms":conditions, "min_quantity":min_quantity, "min_unit":min_unit,
            "is_one_per_patient":one_per,
            "is_new_customer_required":bool(master.get("is_new_customer_required")),
            "is_membership_required":bool(master.get("is_membership_required"))}


def _protected(protections: dict) -> dict[str, set[int]]:
    result = {PROMOTIONS: set(protections.get("promotion_ids", [])),
              OFFERS: set(protections.get("offer_ids", [])),
              ITEMS: set(protections.get("item_ids", []))}
    for entry in protections.get("entries", []):
        if isinstance(entry, dict) and entry.get("table") in result:
            result[entry["table"]].add(entry.get("id"))
    return result


def build_plan(source: dict, graph: dict, evidence: dict, protections: dict) -> dict:
    """Build a reviewable plan without mutating any argument or database.

    Source-wide ambiguity, invalid relationships,
    and touched protected rows block every operation. Bad/image-only offer
    evidence instead permits validated additive work and inhibits all removal.
    """
    plan = {"schema_version": 1, "status": "ready", "source": deepcopy(source),
            "before": deepcopy(graph), "operations": [], "review": [], "notes": [],
            "evidence_summary": {"complete": evidence.get("complete") is True,
                                 "accessible": evidence.get("accessible") is True}}
    if evidence.get("image_policy") is not None:
        plan["image_policy"] = deepcopy(evidence["image_policy"])
        plan["image_retry_reason"] = evidence.get("image_retry_reason")
    operations = plan["operations"]
    next_ids = {PROMOTIONS: -1, OFFERS: -1, ITEMS: -1}

    def emit(table: str, values: dict, existing: dict | None = None) -> int:
        pk = ("promotion_id" if table == PROMOTIONS else "offer_item_id" if table == ITEMS
              else "offer_id" if table == TERMS else "id")
        if existing is None:
            row_id = values["offer_id"] if table == TERMS else next_ids[table]
            if table != TERMS:
                next_ids[table] -= 1
            operations.append({"table": table, "action": "insert", "id": row_id, "values": values})
        else:
            row_id = existing[pk]
            changes = {k: v for k, v in values.items() if existing.get(k) != v}
            if changes:
                operations.append({"table": table, "action": "update", "id": row_id, "values": changes})
        return row_id

    def warning(reason: str, index: int | None = None) -> None:
        plan["review"].append({"reason": reason, "offer_index": index, "blocking": False})
        plan["evidence_summary"]["complete"] = False

    try:
        business_id = _id(source.get("business_id"))
        url = normalize_url(source.get("url"))
        parsed = urlparse(url)
        if parsed.scheme not in {"http", "https"} or not parsed.netloc:
            raise _Review("invalid_source_url")
        source_key = normalize_source_url(source.get("url"))
        if normalize_source_url(evidence.get("url")) != source_key:
            raise _Review("evidence_source_mismatch")
        for key in ("promotions", "offers", "items", "services"):
            if not isinstance(graph.get(key), list) or any(not isinstance(r, dict) for r in graph[key]):
                raise _Review(f"invalid_graph:{key}")
        promotions, offers, items, services = (graph[k] for k in ("promotions", "offers", "items", "services"))
        for rows, pk in ((promotions, "promotion_id"), (offers, "id"), (items, "offer_item_id"), (services, "service_id")):
            ids = [_id(r.get(pk)) for r in rows]
            if len(ids) != len(set(ids)):
                raise _Review("duplicate_graph_id")
        promotion_ids = {p["promotion_id"] for p in promotions}
        offer_ids = {o["id"] for o in offers}
        if any(p.get("business_id") != business_id or normalize_source_url(p.get("source_url")) != source_key for p in promotions):
            raise _Review("source_business_mismatch")
        if any(o.get("business_id") != business_id or o.get("promotion_id") not in promotion_ids or o.get("membership_plan_id") is not None for o in offers):
            raise _Review("invalid_offer_relationship")
        if any(i.get("offer_id") not in offer_ids for i in items) or any(s.get("business_id") != business_id for s in services):
            raise _Review("invalid_item_or_service_relationship")
        source_promotion_id = source.get("promotion_id")
        if source_promotion_id is not None and _id(source_promotion_id) not in promotion_ids:
            raise _Review("source_promotion_not_found")
        markdown = str(evidence.get("markdown") or "")
        observed_date = _date(evidence.get("observed_date"))
        coverage = evidence.get("breakpoint_coverage")
        if coverage is not None and (set(coverage) != {"desktop", "tablet", "mobile"}
                or not all(row.get("complete") is True for row in coverage.values())):
            warning("breakpoint_coverage_incomplete")
        for review in evidence.get("semantic_reviews") or []:
            warning(review.get("reason", "purchase_semantics_requires_review"))
        raw_offers = evidence.get("offers")
        if not isinstance(raw_offers, list):
            raise _Review("invalid_evidence_offers")
        _check_variant_reconciliation(evidence, raw_offers, warning)
        empty_quote = str(evidence.get("no_current_offers_quote") or "").strip()
        explicitly_empty = (not raw_offers and evidence.get("explicitly_empty") is True
                            and plan["evidence_summary"]["complete"] and bool(empty_quote)
                            and _text(empty_quote) in _text(markdown)
                            and bool(re.search(r"\bno\b.{0,50}\b(?:specials?|promotions?|offers?)\b|\b(?:specials?|promotions?|offers?)\b.{0,50}\bnot\s+(?:currently\s+)?available\b", empty_quote, re.I)))
        current_quotes = [_text(o.get("offer_raw_text")) for o in offers if o.get("is_active") is True]
        page_campaign = _campaign({}, evidence)
        page_expired_matches = []
        if not raw_offers and observed_date and page_campaign["campaign_end_date"] and page_campaign["campaign_end_date"] < observed_date:
            page_expired_matches = [p["promotion_id"] for p in promotions if p.get("is_active") is True
                                    and p.get("campaign_end_date") == page_campaign["campaign_end_date"]
                                    and (not page_campaign["promotion_title"] or _text(p.get("promotion_title")) == _text(page_campaign["promotion_title"]))]
            if len(page_expired_matches) != 1:
                warning("expired_campaign_identity_requires_review")
                page_expired_matches = []
        page_without_offers_verified = (evidence.get("complete") is True
                                        and evidence.get("all_offers_extracted") is True
                                        and evidence.get("accessible") is True
                                        and evidence.get("image_review_complete") is True
                                        and (bool(page_expired_matches) or all(q and q not in _text(markdown) for q in current_quotes)))
        page_empty_verified = not raw_offers and page_without_offers_verified
        nothing_live = (not any(p.get("is_active") is True for p in promotions)
                        and not any(o.get("is_active") is True for o in offers))
        # An accessible page without offers cannot change a source that has nothing live: no-op, not review.
        if not raw_offers and plan["evidence_summary"]["accessible"] and markdown.strip() and nothing_live:
            plan["notes"].append({"reason": "no_offers_and_no_live_rows", "blocking": False})
            return plan
        if not plan["evidence_summary"]["accessible"] or (not markdown.strip() and not page_empty_verified) or (not raw_offers and not explicitly_empty and not page_empty_verified):
            raise _Review("inaccessible_empty_or_unresolved_evidence")
        if evidence.get("image_policy", {}).get("hold_parent_fields"):
            warning("image_candidates_shadow_or_unresolved")
        existing_items = {o["id"]: [i for i in items if i["offer_id"] == o["id"]] for o in offers}
        expected: list[tuple[dict, list[dict], dict, dict, int]] = []
        expired_campaigns = list(page_expired_matches)
        non_promo = 0
        for index, raw in enumerate(raw_offers):
            if not isinstance(raw, dict):
                warning("invalid_offer_evidence", index)
                continue
            verified_image = raw.get("visibility") == "image_only" and (
                evidence.get("image_review_complete") is True or
                (evidence.get("image_policy", {}).get("version") == 1 and raw.get("dual_read_verified") is True))
            if raw.get("visibility") != "visible_match" and not verified_image:
                reason = "extracted_quote_not_in_verified_text" if evidence.get("prior_visual_receipt") else "unresolved_image_or_visibility"
                warning(reason, index)
                continue
            try:
                resolved_items = _resolve_items(raw, services, business_id)
                master, reason = _validated_offer(raw, markdown, resolved_items)
                if master is None and reason == "no_supported_promo_fact":
                    non_promo += 1
                    plan["notes"].append({"reason": reason, "offer_index": index, "blocking": False})
                    continue
                if master is None:
                    warning(reason, index)
                    continue
                if not is_blanket_offer_text(master["offer_raw_text"]) and any(i["service_id"] is None for i in resolved_items):
                    plan["notes"].append({"reason": "service_mapping_pending", "offer_index": index, "blocking": False})
                campaign = _campaign(raw, evidence)
                if observed_date and campaign["campaign_end_date"] and campaign["campaign_end_date"] < observed_date:
                    matches = [p for p in promotions if p.get("is_active") is True
                               and p.get("campaign_end_date") == campaign["campaign_end_date"]
                               and (not campaign["promotion_title"] or _text(p.get("promotion_title")) == _text(campaign["promotion_title"]))]
                    if len(matches) != 1:
                        warning("expired_campaign_evidence" if not matches else "expired_campaign_identity_requires_review", index)
                    else:
                        expired_campaigns.append(matches[0]["promotion_id"])
                        plan["notes"].append({"reason":"expired_campaign_verified", "promotion_id":matches[0]["promotion_id"], "blocking":False})
                    continue
                if master["price_model"] == "from":
                    if _variant_reconciliation_records(evidence) and not _variant_catalog_complete(evidence):
                        warning("starting_price_variant_set_incomplete", index)
                        continue
                    plan["notes"].append({"reason": "starting_price_variant_set_incomplete",
                                          "offer_index": index, "blocking": False})
                    plan["evidence_summary"]["complete"] = False
                if raw.get("is_membership_required") and raw.get("regular_price") is None:
                    promo_blob = _text(markdown)
                    if _NON_MEMBER.search(promo_blob) or _NON_MEMBER.search(str(raw.get("offer_raw_text") or "")):
                        warning("member_regular_price_missing", index)
                terms = _term_values(master)
                expected.append((master, resolved_items, campaign, terms, index))
            except (_Review, WriterError) as exc:
                warning(str(exc), index)
        # A page whose only extracted lines are regular prices carries no promotion: treat it as an empty page.
        if raw_offers and not expected and non_promo == len(raw_offers):
            if nothing_live:
                plan["notes"].append({"reason": "no_offers_and_no_live_rows", "blocking": False})
                return plan
            page_empty_verified = page_without_offers_verified
        expired_page_verified = bool(expired_campaigns and plan["evidence_summary"]["complete"] and not plan["review"])
        if not expected and not explicitly_empty and not page_empty_verified and not expired_page_verified:
            raise _Review(plan["review"][-1]["reason"] if plan["review"] else "no_validated_offers")
        complete = (plan["evidence_summary"]["complete"] and not plan["review"]
                    and (explicitly_empty or page_empty_verified or expired_page_verified or bool(expected)))
        if not complete:
            plan["evidence_summary"]["complete"] = False
        selected_promotions: dict[tuple, int] = {}
        promotion_quotes: dict[tuple, list[str]] = {}
        successful_campaigns: set[tuple] = set()
        for master, _, campaign, _, _ in expected:
            promotion_quotes.setdefault(_campaign_key(campaign), []).append(master["offer_raw_text"])
        matched: set[int] = set()
        expected_identities: set[tuple] = set()
        for master, desired_items, campaign, terms, offer_index in expected:
            campaign_key = _campaign_key(campaign)
            expected_identity = (campaign_key, _identity(master, desired_items),
                                 _raw_identity(master["offer_raw_text"]))
            if expected_identity in expected_identities:
                raise _Review("duplicate_evidence_offer")
            expected_identities.add(expected_identity)
            if campaign_key not in selected_promotions:
                candidates = promotions
                has_campaign = any(campaign.values())
                if has_campaign:
                    candidates = [p for p in candidates if all(
                        ( _text(p.get(k)) == _text(v) if k == "promotion_title" else p.get(k) == v)
                        for k, v in campaign.items() if v is not None)]
                    if not candidates and not (campaign["campaign_start_date"] or campaign["campaign_end_date"]):
                        candidates = [p for p in promotions if p.get("is_active") is True
                                      and (source_promotion_id is None or p["promotion_id"] == source_promotion_id)]
                elif source_promotion_id is not None:
                    candidates = [p for p in candidates if p["promotion_id"] == source_promotion_id]
                else:
                    candidates = [p for p in candidates if p.get("is_active") is True]
                if len(candidates) > 1:
                    raise _Review("ambiguous_source_campaign")
                existing_promotion = candidates[0] if candidates else None
                if existing_promotion is not None:
                    if existing_promotion.get("is_active") is not True:
                        raise _Review("historical_campaign_requires_review")
                    # Current verified copy may omit a year/date. Null replaces
                    # stale dates; an explicit expired date was rejected above.

                promo_values = {"business_id": business_id,
                                "source_url": existing_promotion["source_url"] if existing_promotion else url,
                                "is_active": True,
                                "promotion_content": _promotion_content(existing_promotion, promotion_quotes[campaign_key], complete),
                                "is_offer_extracted": complete and not plan["notes"] and not any(is_blanket_offer_text(q) for q in promotion_quotes[campaign_key]),
                                **{k: v for k, v in campaign.items() if v is not None or k != "promotion_title"}}
                if evidence.get("image_policy", {}).get("hold_parent_fields"):
                    if existing_promotion is not None:
                        promo_values = {"is_offer_extracted":False}
                    else:
                        # A new parent built only from released text has no old image children to alter.
                        promo_values["is_offer_extracted"] = False
                if existing_promotion is None:
                    promo_values.setdefault("promotion_title", url)
                    promo_values.setdefault("campaign_start_date", None)
                    promo_values.setdefault("campaign_end_date", None)
                selected_promotions[campaign_key] = emit(PROMOTIONS, promo_values, existing_promotion)
            promotion_id = selected_promotions[campaign_key]
            candidates = [o for o in offers if o["promotion_id"] == promotion_id
                          and _offer_match_key(o, existing_items[o["id"]]) == _offer_match_key(master, desired_items)]
            if len(candidates) > 1:
                # Several bundles of one service can coexist; a unique fixed
                # count/threshold preserves its ID without using the changed price.
                quantities = sorted((str(i.get("service_id") or _service_name_key(i.get("item_name"))),
                                     i.get("delivered_units")) for i in desired_items)
                narrower = [o for o in candidates if sorted(
                    (str(i.get("service_id") or _service_name_key(i.get("item_name"))), i.get("delivered_units"))
                    for i in existing_items[o["id"]]) == quantities
                    and _term_values(o)["min_quantity"] == terms["min_quantity"]]
                if len(narrower) == 1:
                    candidates = narrower
            if not candidates and not is_blanket_offer_text(master["offer_raw_text"]):
                candidates = [o for o in offers if o["promotion_id"] == promotion_id
                              and _nullable_item_match(o, _catalog_items(existing_items[o["id"]], services), master, desired_items)]
            if len(candidates) > 1 and raw_offers[offer_index].get("purchase_variant"):
                exact = [o for o in candidates if _raw_identity(o.get("offer_raw_text")) == _raw_identity(master["offer_raw_text"])]
                if len(exact) == 1:
                    candidates = exact
            if len(candidates) > 1:
                warning("ambiguous_offer_match", offer_index)
                continue
            existing_offer = candidates[0] if candidates else None
            if existing_offer is not None and existing_offer.get("is_active") is not True:
                warning("historical_offer_requires_review", offer_index)
                continue
            if existing_offer is None:
                active_same_scope = [o for o in offers if o.get("is_active") is True
                    and o["promotion_id"] == promotion_id
                    and _offer_match_key(o, existing_items[o["id"]])[:2] == _offer_match_key(master, desired_items)[:2]]
                if active_same_scope:
                    warning("offer_identity_or_qualification_requires_review", offer_index)
                    continue
            if existing_offer and existing_offer["id"] in matched:
                warning("ambiguous_offer_match", offer_index)
                continue
            successful_campaigns.add(campaign_key)
            master_values = {**master, "business_id": business_id, "promotion_id": promotion_id, "membership_plan_id": None}
            if existing_offer is not None:
                tier = new_customer_tier_merge_patch(existing_offer, master)
                if tier:
                    master_values = {**existing_offer, **master_values, **tier}
            offer_id = emit(OFFERS, master_values, existing_offer)
            if existing_offer:
                matched.add(offer_id)
            old_items = existing_items.get(offer_id, [])
            blanket = is_blanket_offer_text(master["offer_raw_text"])
            for desired in desired_items:
                matches = [i for i in old_items if _item_key(i, blanket) == _item_key(desired, blanket)]
                if not matches:
                    matches = [i for i in old_items if (i.get("service_id") is None or desired["service_id"] is None)
                               and _service_name_key(_catalog_items([i], services)[0]["item_name"]) == _service_name_key(desired["item_name"])]
                if len(matches) > 1:
                    raise _Review("ambiguous_item_match")
                if matches and desired["service_id"] is None and not blanket:
                    old = matches[0]
                    service = next((s for s in services if s["service_id"] == old.get("service_id")), None)
                    if service and _service_name_key(service["service_name"]) == _service_name_key(desired["item_name"]):
                        desired = {**desired, "service_id": service["service_id"]}
                emit(ITEMS, {**desired, "offer_id": offer_id}, matches[0] if matches else None)
            current_term = next((t for t in graph.get("terms", []) if t.get("offer_id") == offer_id), None)
            term_payload = {**terms, "offer_id": offer_id}
            has_terms = bool(terms["raw_terms"] or terms["min_quantity"] is not None
                             or terms["is_one_per_patient"] or terms["is_new_customer_required"]
                             or terms["is_membership_required"])
            if current_term is not None or has_terms:
                emit(TERMS, term_payload, current_term)
        successful_promotion_ids = {selected_promotions[key] for key in successful_campaigns}
        operations[:] = [op for op in operations if op["table"] != PROMOTIONS
                         or op["id"] in successful_promotion_ids]
        if plan["review"]:
            complete = False
            plan["evidence_summary"]["complete"] = False
            # Matching can discover uncertainty after the Promotion was emitted.
            # Clear the completion flag while retaining independent safe changes.
            for promotion_id in successful_promotion_ids:
                op = next((op for op in operations if op["table"] == PROMOTIONS
                           and op["id"] == promotion_id), None)
                if op is not None:
                    op["values"]["is_offer_extracted"] = False
                else:
                    existing = next(p for p in promotions if p["promotion_id"] == promotion_id)
                    emit(PROMOTIONS, {"is_offer_extracted": False}, existing)
        if complete:
            for offer in offers:
                if offer.get("is_active") is True and offer["id"] not in matched and (expected or not expired_page_verified or offer["promotion_id"] in expired_campaigns):
                    emit(OFFERS, {"is_active": False}, offer)
            for promotion in promotions:
                if promotion.get("is_active") is True and promotion["promotion_id"] not in selected_promotions.values() and (expected or not expired_page_verified or promotion["promotion_id"] in expired_campaigns):
                    emit(PROMOTIONS, {"is_active": False, "is_offer_extracted": False}, promotion)
        elif page_empty_verified or explicitly_empty:
            for offer in offers:
                if offer.get("is_active") is True:
                    emit(OFFERS, {"is_active": False}, offer)
            for promotion in promotions:
                if promotion.get("is_active") is True:
                    emit(PROMOTIONS, {"is_active": False, "is_offer_extracted": False}, promotion)
        protected = _protected(protections)
        offer_lookup = {o["id"]: o for o in offers}
        item_lookup = {i["offer_item_id"]: i for i in items}
        for op in operations:
            table, row_id = op["table"], op["id"]
            touches = {(table, row_id)}
            if table == OFFERS:
                touches.update((ITEMS, i["offer_item_id"]) for i in existing_items.get(row_id, []))
                parent_id = op["values"].get("promotion_id", offer_lookup.get(row_id, {}).get("promotion_id"))
                touches.add((PROMOTIONS, parent_id))
            if table == ITEMS:
                parent_id = op["values"].get("offer_id", item_lookup.get(row_id, {}).get("offer_id"))
                touches.add((OFFERS, parent_id))
                touches.add((PROMOTIONS, offer_lookup.get(parent_id, {}).get("promotion_id")))
            if table == PROMOTIONS:
                children = ([o for o in offers if o["promotion_id"] == row_id]
                            if set(op["values"]) - {"is_offer_extracted"} else [])
                touches.update((OFFERS, o["id"]) for o in children)
                touches.update((ITEMS, i["offer_item_id"]) for o in children for i in existing_items[o["id"]])
            if any(touched_table in protected and touched_id in protected[touched_table]
                   for touched_table, touched_id in touches):
                raise _Review("protected_row_or_cascade")
        if plan["review"] and not operations:
            plan["status"] = "review"
    except (_Review, WriterError) as exc:
        plan["status"] = "review"
        plan["operations"] = []
        plan["review"].append({"reason": str(exc), "blocking": True})
    return plan
