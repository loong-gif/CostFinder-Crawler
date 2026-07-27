"""Create missing service-catalog identities from exact CSV promotion evidence."""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Iterable, Mapping

from utils.offer_extraction_llm import canonicalize_service_name
from utils.offer_fingerprint import normalize_offer_raw_text
from utils.promo_csv_import import normalize_source_url

_VALID_UNITS = frozenset({"unit", "syringe", "half_syringe", "vial", "treatment", "session", "package", "area", "ml", "mg", "others"})


@dataclass(frozen=True)
class CatalogBackfillPlan:
    services: list[dict[str, Any]]
    links: list[dict[str, Any]]
    skipped: list[dict[str, Any]]


def _canonical(value: Any, evidence: str = "") -> str:
    return str(canonicalize_service_name(value, evidence) or "").strip()


def _offer_url(offer: Mapping[str, Any]) -> str:
    promotion = offer.get("clinic_promotions")
    if isinstance(promotion, Mapping):
        return normalize_source_url(promotion.get("source_url"))
    if isinstance(promotion, list) and len(promotion) == 1 and isinstance(promotion[0], Mapping):
        return normalize_source_url(promotion[0].get("source_url"))
    return ""


def _unit(value: Any) -> str:
    text = str(value or "").strip().lower().replace("-", "_").replace(" ", "_")
    if "unit" in text:
        return "unit"
    return text if text in _VALID_UNITS else "others"


def _category(service_name: str, value: Any) -> str:
    if service_name in {"Botox", "Dysport", "Daxxify", "Letybo", "Xeomin", "Jeuveau", "Neurotoxin"}:
        return "Neurotoxin"
    if service_name in {"Dermal Filler", "Juvéderm", "Restylane", "Belotero", "Radiesse", "Sculptra", "Kybella", "Skinvive"}:
        return "Filler"
    text = str(value or "").strip().lower()
    if "neuro" in text or "tox" in text:
        return "Neurotoxin"
    if "filler" in text or "injectable" in text:
        return "Filler"
    return "others"


def _csv_evidence(rows: Iterable[Mapping[str, Any]]) -> dict[tuple[str, str], list[dict[str, str]]]:
    out: dict[tuple[str, str], list[dict[str, str]]] = {}
    for row in rows:
        raw_text = str(row.get("offer_raw_text") or "")
        key = (normalize_offer_raw_text(raw_text), normalize_source_url(row.get("source_url")))
        name = _canonical(row.get("service_name"), raw_text)
        if not key[0] or not key[1] or not name or name in {"Others", "Package"}:
            continue
        evidence = {
            "service_name": name,
            "service_category": _category(name, row.get("service_category")),
            "unit_type": _unit(row.get("unit_type")),
            "source_url": key[1],
            "service_name_raw": str(row.get("service_name") or "").strip() or name,
        }
        if evidence not in out.setdefault(key, []):
            out[key].append(evidence)
    return out


def build_catalog_backfill_plan(
    offers: Iterable[Mapping[str, Any]],
    services: Iterable[Mapping[str, Any]],
    csv_rows: Iterable[Mapping[str, Any]],
) -> CatalogBackfillPlan:
    """Plan only exact-text-and-URL catalog identities absent from each business."""
    evidence_by_offer = _csv_evidence(csv_rows)
    existing = {(row.get("business_id"), _canonical(row.get("service_name"))) for row in services if row.get("service_id") is not None}
    services_by_key: dict[tuple[int, str], dict[str, Any]] = {}
    links: list[dict[str, Any]] = []
    skipped: list[dict[str, Any]] = []
    for offer in offers:
        business_id = offer.get("business_id")
        items = offer.get("promo_offer_items")
        item_rows = items if isinstance(items, list) else [items] if isinstance(items, Mapping) else []
        unlinked = [item for item in item_rows if isinstance(item, Mapping) and item.get("service_id") is None]
        if not isinstance(business_id, int):
            continue
        evidence = evidence_by_offer.get((normalize_offer_raw_text(offer.get("offer_raw_text")), _offer_url(offer)), [])
        names = {row["service_name"] for row in evidence}
        for item in unlinked:
            item_id = item.get("offer_item_id")
            if not isinstance(item_id, int):
                continue
            if len(names) != 1:
                skipped.append({"offer_item_id": item_id, "reason": "missing_csv_evidence" if not names else "ambiguous_csv_evidence"})
                continue
            row = evidence[0]
            key = (business_id, row["service_name"])
            if key in existing:
                skipped.append({"offer_item_id": item_id, "reason": "service_already_exists"})
                continue
            services_by_key.setdefault(key, {"business_id": business_id, **row})
            links.append({"offer_item_id": item_id, "business_id": business_id, "service_name": row["service_name"]})
    return CatalogBackfillPlan(services=list(services_by_key.values()), links=links, skipped=skipped)
