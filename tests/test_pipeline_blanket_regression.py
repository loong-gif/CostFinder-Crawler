"""Offline regression cases for source titles and blanket persistence."""
from scripts.apply_pipeline_bundle import insert_offer_items_live
from scripts.run_domain_architecture_pipeline import link_item_services
from utils.extraction_persist import build_master_from_offer, persist_promo_offer, route_offer
from utils.promo_offer_items_db import upsert_offer_items


RAW = "10% off all services"


def blanket_offer():
    return {"offer_raw_text": RAW, "discount_percent": 10, "conditions": [],
            "is_membership_required": False, "is_new_customer_required": False,
            "items": [{"service_name": "Dermal Filler"}, {"service_name": "Sculptra"}]}


class MemoryClient:
    def __init__(self, existing=None):
        self.masters = list(existing or [])
        self.items = []
        self.writes = []
        self.promotion = {"promotion_id": 2, "business_id": 1, "is_active": True}

    def fetch_rows(self, table, select, **kwargs):
        if table == "clinic_promotions":
            return [self.promotion]
        if table == "promo_offer_master":
            rows = self.masters
            for field, expression in kwargs.get("filters", {}).items():
                if expression == "is.null":
                    rows = [row for row in rows if row.get(field) is None]
                elif expression == "not.is.null":
                    rows = [row for row in rows if row.get(field) is not None]
                elif expression.startswith("eq."):
                    value = expression[3:]
                    rows = [row for row in rows if str(row.get(field)).lower() == value.lower()]
            return [{**row, "promo_offer_items": [item for item in self.items if item["offer_id"] == row["id"]]} for row in rows]
        if table == "promo_offer_items":
            offer_id = int(kwargs["filters"]["offer_id"].split(".")[-1])
            return [row for row in self.items if row["offer_id"] == offer_id]
        if table == "clinic_memberships":
            return [{"plan_id": 8, "business_id": 1}]
        if table == "clinic_services":
            return [{"service_id": 7, "business_id": 1, "service_name": "Dermal Filler"}]
        raise AssertionError(table)

    def insert_rows(self, table, rows):
        self.writes.append(("insert", table, rows))
        if table == "promo_offer_master":
            stored = {"id": 42, **rows[0]}
            self.masters.append(stored)
            return [stored]
        self.items.extend(rows)
        return rows

    def update_row(self, table, filters, payload):
        self.writes.append(("update", table, payload))
        if table == "clinic_promotions":
            self.promotion.update(payload)
        if table == "promo_offer_master":
            offer_id = int(filters["id"].split(".")[-1])
            for row in self.masters:
                if row["id"] == offer_id:
                    row.update(payload)
        return [payload]

    def delete_rows(self, table, filters):
        self.writes.append(("delete", table, filters))
        if table == "promo_offer_items":
            offer_id = int(filters["offer_id"].split(".")[-1])
            self.items = [item for item in self.items if item["offer_id"] != offer_id]


def test_blanket_builder_collapses_service_items_into_one_unlinked_scope():
    _, items = build_master_from_offer(blanket_offer(), business_id=1, promotion_id=2, source_url="https://example.com/specials")
    assert items == [{"item_name": RAW}]


def test_blanket_product_discount_is_a_promotion_not_retail_exclusion():
    assert route_offer({"service_name": "Skincare Product", "offer_raw_text": "20% off all products", "discount_percent": 20}) == "promo"


def test_blanket_twice_writes_one_offer_and_no_second_semantic_change():
    client = MemoryClient()
    args = dict(business_id=1, promotion_id=2, source_url="https://example.com/specials", offer=blanket_offer(), evidence=RAW)
    first = persist_promo_offer(client, **args)
    count = len(client.writes)
    second = persist_promo_offer(client, **args)
    assert first["accepted"] is True
    assert len(client.masters) == 1
    assert len(client.items) == 1
    assert client.items[0].get("service_id") is None
    assert client.items[0]["item_name"] == RAW
    assert second["action"] == "skipped"
    assert len(client.writes) == count


def test_inactive_blanket_duplicate_is_not_reactivated():
    client = MemoryClient([{"id": 10542, "business_id": 1, "promotion_id": 2, "membership_plan_id": None, "offer_raw_text": RAW, "is_active": False}])
    result = persist_promo_offer(client, business_id=1, promotion_id=2, source_url="https://example.com/specials", offer=blanket_offer(), evidence=RAW)
    assert result["action"] == "skipped"
    assert client.writes == []


def test_domain_service_relinker_skips_existing_blanket(monkeypatch):
    client = MemoryClient([{"id": 42, "offer_raw_text": RAW, "promo_offer_items": [{"offer_item_id": 7, "service_id": None}]}])
    monkeypatch.setattr("scripts.run_domain_architecture_pipeline.fetch_service_row_by_enum", lambda *args: (_ for _ in ()).throw(AssertionError("blanket must not resolve a service")))
    assert link_item_services(client, business_id=1) == 0
    assert client.writes == []


def test_bundle_item_writer_preserves_specific_title_when_mapped():
    client = MemoryClient()
    insert_offer_items_live(client, 42, [{"service_id": 7, "item_name": "Lip Filler"}])
    assert client.items[0]["item_name"] == "Lip Filler"


def test_same_items_do_not_replace_rows_or_touch_updated_at():
    client = MemoryClient()
    items = [{"service_id": 7, "item_name": "Lip Filler"}]
    upsert_offer_items(client, 42, items)
    count = len(client.writes)
    upsert_offer_items(client, 42, items)
    assert len(client.writes) == count


def test_bundle_persistence_uses_shared_blanket_guard_twice():
    from scripts.apply_pipeline_bundle import persist_promotion_and_offers_from_bundle
    client = MemoryClient()
    args = dict(business_id=1, promotion={"source_url": "https://example.com/specials", "promotion_description": RAW}, offers=[blanket_offer()], membership_plan_ids=[])
    first = persist_promotion_and_offers_from_bundle(client, **args)
    count = len(client.writes)
    second = persist_promotion_and_offers_from_bundle(client, **args)
    assert first["offers"] == 1
    assert second["offers"] == 0
    assert len(client.writes) == count
    assert client.items[0].get("service_id") is None


def test_bundle_rejects_offer_without_matching_source_evidence():
    from scripts.apply_pipeline_bundle import persist_promotion_and_offers_from_bundle
    client = MemoryClient()
    result = persist_promotion_and_offers_from_bundle(client, business_id=1, promotion={"source_url": "https://example.com/specials", "promotion_description": "Contact our clinic."}, offers=[blanket_offer()], membership_plan_ids=[])
    assert result["offers"] == 0
    assert client.writes == []


def test_specific_offer_twice_ignores_database_recomputed_fingerprint():
    from utils.extraction_persist import route_and_persist_extraction
    client = MemoryClient()
    raw = "Lip Filler special $199"
    offer = {"offer_raw_text": raw, "discount_price": 199, "conditions": [], "items": [{"service_name": "Dermal Filler", "item_name": "Lip Filler"}]}
    args = dict(business_id=1, promotion_id=2, source_url="https://example.com/specials", offers=[offer], evidence=raw)
    first = route_and_persist_extraction(client, **args)
    client.masters[0]["offer_fingerprint"] = "database_items_and_prices_fingerprint"
    count = len(client.writes)
    second = route_and_persist_extraction(client, **args)
    assert first["promos"] == 1
    assert second["skipped"] == 1
    assert len(client.writes) == count
    assert client.items[0]["item_name"] == "Lip Filler"


def test_changed_blanket_discount_repairs_existing_active_offer():
    client = MemoryClient([{"id": 42, "business_id": 1, "promotion_id": 2, "membership_plan_id": None, "offer_raw_text": RAW, "is_active": True, "conditions": [], "price_model": "total", "is_package": False, "is_new_customer_required": False, "is_membership_required": False, "discount_percent": 20}])
    client.items = [{"offer_id": 42, "item_name": RAW}]
    result = persist_promo_offer(client, business_id=1, promotion_id=2, source_url="https://example.com/specials", offer=blanket_offer(), evidence=RAW)
    assert result["action"] == "updated"
    assert len(client.masters) == 1
    assert client.masters[0]["discount_percent"] == 10


def test_item_noop_compares_delivered_units_read_alias():
    client = MemoryClient()
    client.items = [{"offer_id": 42, "service_id": 7, "item_name": "Lip Filler", "quantity": 2, "unit_price": 200}]
    upsert_offer_items(client, 42, [{"service_id": 7, "item_name": "Lip Filler", "quantity": 2, "unit_price": 200}])
    assert client.writes == []


def test_same_source_package_retains_all_items_and_second_run_is_unchanged(monkeypatch):
    from utils.extraction_persist import route_and_persist_extraction
    raw = "Sculptra and PRF package special $999"
    common = {"offer_raw_text": raw, "discount_price": 999, "conditions": []}
    offers = [{**common, "items": [{"service_name": name, "item_name": name}]} for name in ("Sculptra", "PRF")]
    monkeypatch.setattr("utils.extraction_persist.attach_service_ids_to_items", lambda client, **kw: [dict(item, service_id=index + 7) for index, item in enumerate(kw["items"])])
    client = MemoryClient()
    original_fetch = client.fetch_rows
    client.fetch_rows = lambda table, select, **kw: [{"service_id": 7, "business_id": 1, "service_name": "Sculptra"},
        {"service_id": 8, "business_id": 1, "service_name": "PRF"}] if table == "clinic_services" else original_fetch(table, select, **kw)
    args = dict(business_id=1, promotion_id=2, source_url="https://example.com/specials", offers=offers, evidence=raw)
    first = route_and_persist_extraction(client, **args)
    count = len(client.writes)
    second = route_and_persist_extraction(client, **args)
    assert first["promos"] == 1
    assert len(client.items) == 2
    assert client.masters[0]["is_package"] is True
    assert all(item.get("unit_price") is None for item in client.items)
    assert {item["item_name"] for item in client.items} == {"Sculptra", "PRF"}
    assert second["skipped"] == 1
    assert len(client.writes) == count


def test_domain_promotion_offer_pipeline_twice_has_no_second_write(monkeypatch):
    from scripts.run_domain_architecture_pipeline import persist_promotions_and_offers
    client = MemoryClient()
    monkeypatch.setattr("scripts.run_domain_architecture_pipeline.llm_extract", lambda llm, schema, **kw: {"promotions": [{"promotion_title": "October Specials", "promotion_content": [RAW]}]} if schema["kind"] == "promotion" else {"offers": [blanket_offer()]})
    url = "https://example.com/specials"
    args = dict(business_id=1, scrapes=[{"source_url": url, "markdown": "# October Specials\n" + RAW}], llm=object(), promo_schema={"kind": "promotion"}, offer_schema={"kind": "offers"}, membership_plan_ids=[], rendered_visibility={url: {"status": "page_accessible", "claimResults": [{"recordType": "promotion", "claim": RAW, "status": "visible_match"}]}})
    first = persist_promotions_and_offers(client, **args)
    client.masters[0]["offer_fingerprint"] = "database_items_and_prices_fingerprint"
    count = len(client.writes)
    second = persist_promotions_and_offers(client, **args)
    assert first["offers"] == 1
    assert second["offers"] == 0
    assert len(client.writes) == count


def test_live_derived_comparisons_do_not_cause_repeat_writes():
    raw = "Lip Filler Regular $300 Special $200"
    client = MemoryClient([{"id": 42, "business_id": 1, "promotion_id": 2, "membership_plan_id": None, "offer_raw_text": raw, "is_active": True, "conditions": [], "price_model": "total", "is_new_customer_required": False, "is_membership_required": False, "regular_price": 300, "discount_price": 200, "discount_amount": 100, "discount_percent": 33.33, "is_package": False}])
    client.items = [{"offer_id": 42, "service_id": 7, "item_name": "Lip Filler"}]
    offer = {"offer_raw_text": raw, "regular_price": 300, "discount_price": 200, "conditions": [], "items": [{"service_name": "Dermal Filler", "item_name": "Lip Filler"}]}
    offer.update(is_membership_required=False, is_new_customer_required=False)
    result = persist_promo_offer(client, business_id=1, promotion_id=2, source_url="https://example.com/specials", offer=offer, evidence=raw)
    assert result["action"] == "skipped"
    assert client.writes == []
    assert "discount_amount" not in offer and "discount_percent" not in offer


def test_membership_offer_selects_only_membership_source_and_stays_idempotent():
    raw = "Members receive 10% off all services"
    offer = {"offer_raw_text": raw, "discount_percent": 10, "is_membership_required": True, "conditions": [], "items": [{"service_name": "Dermal Filler"}]}
    client = MemoryClient()
    args = dict(business_id=1, promotion_id=2, membership_plan_id=8, source_url="https://example.com/membership", offer=offer, evidence=raw)
    first = persist_promo_offer(client, **args)
    count = len(client.writes)
    second = persist_promo_offer(client, **args)
    assert first["accepted"] is True
    assert client.masters[0]["membership_plan_id"] == 8
    assert client.masters[0]["promotion_id"] is None
    assert second["action"] == "skipped"
    assert len(client.writes) == count


def test_blanket_predicate_covers_live_wording_without_treatment_false_positive():
    from utils.promo_offer_items_db import is_blanket_offer_text
    for text in ("15% off all regular-priced services", "10% off products & services", "10% off all medspa services", "20% off all purchases"):
        assert is_blanket_offer_text(text)
        _, items = build_master_from_offer({"offer_raw_text": text, "discount_percent": 10, "conditions": [], "items": [{"service_name": "Neurotoxin"}, {"service_name": "Dermal Filler"}]}, business_id=1, promotion_id=2, source_url="https://example.com/specials")
        assert items == [{"item_name": text}]
    assert not is_blanket_offer_text("10% off Neurotoxin")
