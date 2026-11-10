from __future__ import annotations

from typing import Any, Dict, List, Optional, Sequence
from urllib.parse import urlparse

from utils.domain_pipeline_search import PROMO_PATH_RE, llm_extract
from utils.extraction_persist import route_and_persist_extraction
from utils.extraction_persist_pricing import build_master_from_offer
from utils.extraction_persist_records import persist_promotion_item
from utils.clinic_service_taxonomy import SERVICE_NAME_ENUM_VALUES, service_name_enum_hint
from utils.clinic_services_db import fetch_service_row_by_enum
from utils.clinic_services_search import SearchPage, filter_service_menu_urls
from utils.membership_paths import is_membership_page_url
from utils.promo_offer_items_db import is_blanket_offer_text, link_item_to_service
from utils.schema_contract import TABLE_PROMO_OFFER_MASTER


def persist_memberships(
    client: Any,
    *,
    business_id: int,
    pages: Sequence[SearchPage],
    llm: Any,
    schema: dict[str, Any],
    rendered_visibility: dict[str, dict[str, Any]] | None = None,
) -> List[int]:
    plan_ids: List[int] = []
    membership_pages = [p for p in pages if is_membership_page_url(p.url)] or list(pages[:1])
    for page in membership_pages:
        payload = llm_extract(
            llm,
            schema,
            task="Extract purchasable membership plans with explicit prices.",
            source_url=page.url,
            markdown=page.markdown,
        )
        for item in payload.get("memberships") or []:
            from utils.extraction_persist_records import persist_membership_item
            result = persist_membership_item(client, business_id=business_id, item=item, source_url=page.url, evidence=page.markdown, rendered_visibility=(rendered_visibility or {}).get(page.url))
            if result.get("accepted"):
                plan_ids.append(int(result["plan_id"]))
    return plan_ids


def persist_services(
    client: Any,
    *,
    business_id: int,
    pages: Sequence[SearchPage],
    llm: Any,
    schema: dict[str, Any],
    domain: str,
) -> List[int]:
    service_ids: List[int] = []
    from utils.clinic_service_extraction import upsert_extracted_service

    candidates = [
        p
        for p in pages
        if not is_membership_page_url(p.url) and not PROMO_PATH_RE.search(urlparse(p.url).path or "")
    ]
    service_pages = filter_service_menu_urls(candidates, domain=domain) or candidates
    seen_names: set[str] = set()
    for page in service_pages[:4]:
        payload = llm_extract(
            llm,
            schema,
            task=(
                "Extract explicit regular (non-promotional) service unit prices only. "
                "service_name must be exactly one of: " + ", ".join(SERVICE_NAME_ENUM_VALUES) + ". "
                "Do not infer a canonical category; service_category_raw is optional and must be copied verbatim from a source label."
            ),
            source_url=page.url,
            markdown=page.markdown,
        )
        for item in payload.get("services") or []:
            service_enum = service_name_enum_hint(item.get("service_name"))
            if service_enum is None:
                continue
            service_key = service_enum
            if service_key in seen_names:
                continue
            seen_names.add(service_key)
            write = upsert_extracted_service(
                client,
                business_id=business_id,
                item=item,
                source_url=page.url,
                evidence=page.markdown,
            )
            if write.get("service_id"):
                service_ids.append(int(write["service_id"]))
    return service_ids


def schema_offer_to_master(
    offer: dict[str, Any],
    *,
    business_id: int,
    promotion_id: int,
    source_url: str,
    membership_plan_id: Optional[int],
) -> tuple[dict[str, Any], List[dict[str, Any]]]:
    return build_master_from_offer(
        offer,
        business_id=business_id,
        promotion_id=promotion_id,
        source_url=source_url,
        membership_plan_id=membership_plan_id,
    )


def persist_promotions_and_offers(
    client: Any,
    *,
    business_id: int,
    scrapes: Sequence[Dict[str, Any]],
    llm: Any,
    promo_schema: dict[str, Any],
    offer_schema: dict[str, Any],
    membership_plan_ids: Sequence[int],
    rendered_visibility: dict[str, dict[str, Any]],
) -> Dict[str, Any]:
    stats = {"promotions": 0, "offers": 0, "items": 0}
    default_plan_id = membership_plan_ids[0] if membership_plan_ids else None
    for scrape in scrapes:
        md = str(scrape.get("markdown") or "")
        url = str(scrape.get("source_url") or "")
        if not md:
            continue
        promo_payload = llm_extract(
            llm,
            promo_schema,
            task="Extract concrete clinic promotions; empty array if none.",
            source_url=url,
            markdown=md,
        )
        promos = promo_payload.get("promotions") or []
        if not promos:
            continue
        promo_write = persist_promotion_item(
            client,
            business_id=business_id,
            item=promos[0],
            source_url=url,
            evidence=md,
            rendered_visibility=rendered_visibility.get(url),
        )
        if not promo_write.get("accepted"):
            continue
        promotion_id = int(promo_write["promotion_id"])
        stats["promotions"] += 1
        offers_payload = llm_extract(
            llm,
            offer_schema,
            task=(
                "Extract every concrete purchasable offer on this promotion page. "
                "OR alternatives (or/and/,/或, one shared price/discount): purchase_type=independent_options or split singles, is_package=false, one item_name per option. "
                "AND bundle (one total for multiple treatments): purchase_type=bundle, is_package=true, price_model=total, separate items[] per component, item unit_price=null."
            ),
            source_url=url,
            markdown=md,
        )
        existing = client.fetch_rows(
            TABLE_PROMO_OFFER_MASTER,
            "offer_fingerprint",
            filters={"business_id": f"eq.{business_id}", "promotion_id": f"eq.{promotion_id}"},
            limit=500,
        )
        seen = {str(row.get("offer_fingerprint") or "") for row in existing if row.get("offer_fingerprint")}
        routed = route_and_persist_extraction(
            client,
            business_id=business_id,
            promotion_id=promotion_id,
            source_url=url,
            offers=offers_payload.get("offers") or [],
            evidence=md,
            membership_plan_id=default_plan_id,
            seen_fingerprints=seen,
        )
        stats["offers"] += routed["promos"]
        stats["items"] += sum(
            write.get("items", 0) for write in routed["promo_writes"] if write.get("accepted")
        )
    return stats


def link_item_services(client: Any, *, business_id: int) -> int:
    linked = 0
    offers = client.fetch_rows(
        TABLE_PROMO_OFFER_MASTER,
        "id,offer_raw_text,promo_offer_items(offer_item_id,service_id)",
        filters={"business_id": f"eq.{business_id}", "is_active": "eq.true"},
        limit=500,
    )
    for offer in offers:
        items = offer.get("promo_offer_items") or []
        if isinstance(items, dict):
            items = [items]
        hint = str(offer.get("offer_raw_text") or "")
        if is_blanket_offer_text(hint):
            continue
        for item in items:
            if item.get("service_id"):
                continue
            from utils.clinic_service_extraction import infer_service_name_for_item
            name = infer_service_name_for_item(
                offer_raw_text=hint,
                item_name=item.get("item_name"),
                service_name=item.get("service_name"),
                sibling_count=len(items),
                is_package=len(items) > 1,
            )
            service_enum = service_name_enum_hint(name)
            if service_enum is None:
                continue
            svc = fetch_service_row_by_enum(client, business_id, service_enum)
            if svc:
                link_item_to_service(client, int(item["offer_item_id"]), int(svc["service_id"]))
                linked += 1
    return linked
