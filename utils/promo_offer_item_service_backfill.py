"""Conservative planning for promo-offer item service foreign-key backfills."""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Iterable, Mapping

from utils.clinic_service_extraction import infer_service_name_for_item
from utils.offer_extraction_llm import canonicalize_service_name
from utils.offer_fingerprint import normalize_offer_raw_text
from utils.promo_csv_import import normalize_source_url


@dataclass(frozen=True)
class BackfillPlan:
    """A write-free plan. ``links`` is safe to apply only after review."""

    links: list[dict[str, Any]]
    skipped: list[dict[str, Any]]


@dataclass(frozen=True)
class CatalogBackfillPlan:
    """Conservative plan to seed missing service identities and link offer items.

    Newly seeded catalog rows intentionally contain only identity fields.  A
    promotional price is never treated as a clinic's regular price.
    """

    creates: list[dict[str, Any]]
    links: list[dict[str, Any]]
    skipped: list[dict[str, Any]]


def _canonical_name(value: Any, evidence: str = "") -> str:
    return str(canonicalize_service_name(value, "", evidence) or "").strip()


def _csv_names_by_evidence(
    csv_rows: Iterable[Mapping[str, Any]],
) -> tuple[dict[tuple[str, str], set[str]], dict[str, set[str]]]:
    by_text_and_url: dict[tuple[str, str], set[str]] = {}
    by_text: dict[str, set[str]] = {}
    for row in csv_rows:
        raw_text = normalize_offer_raw_text(row.get("offer_raw_text"))
        name = _canonical_name(row.get("service_name"), str(row.get("offer_raw_text") or ""))
        if raw_text and name:
            by_text.setdefault(raw_text, set()).add(name)
            source_url = normalize_source_url(row.get("source_url"))
            if source_url:
                by_text_and_url.setdefault((raw_text, source_url), set()).add(name)
    return by_text_and_url, by_text


def _offer_source_url(offer: Mapping[str, Any]) -> str:
    promotion = offer.get("clinic_promotions")
    if isinstance(promotion, Mapping):
        return normalize_source_url(promotion.get("source_url"))
    if isinstance(promotion, list) and len(promotion) == 1 and isinstance(promotion[0], Mapping):
        return normalize_source_url(promotion[0].get("source_url"))
    return ""


def _service_candidates(
    services: Iterable[Mapping[str, Any]], *, business_id: int, service_name: str
) -> list[Mapping[str, Any]]:
    return [
        row
        for row in services
        if row.get("business_id") == business_id
        and _canonical_name(row.get("service_name"), str(row.get("service_name_raw") or "")) == service_name
        and row.get("service_id") is not None
    ]


def build_backfill_plan(
    offers: Iterable[Mapping[str, Any]],
    services: Iterable[Mapping[str, Any]],
    csv_rows: Iterable[Mapping[str, Any]],
) -> BackfillPlan:
    """Return only unique, same-business service links without mutating inputs.

    Exact CSV raw-text evidence is preferred. A single-item offer may fall back to
    the established service-name inference; bundles are deliberately not guessed.
    """
    csv_names_by_url, csv_names_by_text = _csv_names_by_evidence(csv_rows)
    service_rows = tuple(services)
    links: list[dict[str, Any]] = []
    skipped: list[dict[str, Any]] = []
    for offer in offers:
        business_id = offer.get("business_id")
        items = offer.get("promo_offer_items")
        item_rows = items if isinstance(items, list) else [items] if isinstance(items, Mapping) else []
        unlinked = [row for row in item_rows if isinstance(row, Mapping) and row.get("service_id") is None]
        if not isinstance(business_id, int):
            skipped.extend(
                {"offer_item_id": item.get("offer_item_id"), "reason": "invalid_business_id"}
                for item in unlinked
            )
            continue
        raw_text = str(offer.get("offer_raw_text") or "")
        normalized_text = normalize_offer_raw_text(raw_text)
        source_url = _offer_source_url(offer)
        csv_names_for_offer = csv_names_by_url.get((normalized_text, source_url), set())
        csv_reason = "csv_exact_text_and_url"
        if not csv_names_for_offer:
            csv_names_for_offer = csv_names_by_text.get(normalized_text, set())
            csv_reason = "csv_exact_text"
        for item in unlinked:
            item_id = item.get("offer_item_id")
            if not isinstance(item_id, int):
                skipped.append({"offer_item_id": item_id, "reason": "invalid_offer_item_id"})
                continue
            if len(csv_names_for_offer) == 1:
                name = next(iter(csv_names_for_offer))
                reason = csv_reason
            elif len(csv_names_for_offer) > 1:
                skipped.append({"offer_item_id": item_id, "reason": "ambiguous_csv_evidence"})
                continue
            elif len(unlinked) != 1:
                skipped.append({"offer_item_id": item_id, "reason": "multi_item_without_csv_evidence"})
                continue
            else:
                name = _canonical_name(
                    infer_service_name_for_item(
                        offer_raw_text=raw_text,
                        quantity=item.get("quantity"),
                        sibling_count=1,
                    ),
                    raw_text,
                )
                reason = "single_item_inference"
            if not name or name in {"Others", "Package"}:
                skipped.append({"offer_item_id": item_id, "reason": "unresolved_service_name"})
                continue
            candidates = _service_candidates(service_rows, business_id=business_id, service_name=name)
            if len(candidates) != 1:
                skipped.append(
                    {
                        "offer_item_id": item_id,
                        "reason": "missing_service_candidate" if not candidates else "ambiguous_service_candidate",
                        "service_name": name,
                    }
                )
                continue
            links.append({"offer_item_id": item_id, "service_id": int(candidates[0]["service_id"]), "reason": reason})
    return BackfillPlan(links=links, skipped=skipped)


def build_catalog_backfill_plan(
    offers: Iterable[Mapping[str, Any]],
    services: Iterable[Mapping[str, Any]],
) -> CatalogBackfillPlan:
    """Plan same-business links, seeding only unambiguous service identities.

    The offer text is sufficient evidence only for a one-item offer.  Bundles
    remain untouched because a single raw-text field cannot safely identify
    each component.  Existing linked items are never changed.
    """
    service_rows = tuple(services)
    creates_by_key: dict[tuple[int, str], dict[str, Any]] = {}
    links: list[dict[str, Any]] = []
    skipped: list[dict[str, Any]] = []

    for offer in offers:
        business_id = offer.get("business_id")
        items = offer.get("promo_offer_items")
        item_rows = items if isinstance(items, list) else [items] if isinstance(items, Mapping) else []
        unlinked = [row for row in item_rows if isinstance(row, Mapping) and row.get("service_id") is None]
        if not isinstance(business_id, int):
            skipped.extend(
                {"offer_item_id": item.get("offer_item_id"), "reason": "invalid_business_id"}
                for item in unlinked
            )
            continue
        if len(item_rows) != 1:
            skipped.extend(
                {"offer_item_id": item.get("offer_item_id"), "reason": "multi_item_without_csv_evidence"}
                for item in unlinked
            )
            continue
        if not unlinked:
            continue

        item = unlinked[0]
        item_id = item.get("offer_item_id")
        if not isinstance(item_id, int):
            skipped.append({"offer_item_id": item_id, "reason": "invalid_offer_item_id"})
            continue
        raw_text = str(offer.get("offer_raw_text") or "")
        name = _canonical_name(
            infer_service_name_for_item(
                offer_raw_text=raw_text,
                quantity=item.get("quantity"),
                sibling_count=1,
            ),
            raw_text,
        )
        if not name or name in {"Others", "Package"}:
            skipped.append({"offer_item_id": item_id, "reason": "unresolved_service_name"})
            continue

        candidates = _service_candidates(service_rows, business_id=business_id, service_name=name)
        link: dict[str, Any] = {
            "offer_item_id": item_id,
            "business_id": business_id,
            "service_name": name,
        }
        if len(candidates) == 1:
            link.update({"service_id": int(candidates[0]["service_id"]), "reason": "single_item_inference"})
        elif len(candidates) > 1:
            skipped.append({"offer_item_id": item_id, "reason": "ambiguous_service_candidate", "service_name": name})
            continue
        else:
            creates_by_key.setdefault((business_id, name), {"business_id": business_id, "service_name": name})
            link["reason"] = "single_item_inference_created_service"
        links.append(link)

    return CatalogBackfillPlan(
        creates=list(creates_by_key.values()),
        links=links,
        skipped=skipped,
    )
