import json

import pytest

from utils.clinic_service_taxonomy import SERVICE_NAME_ENUM_VALUES
from utils.pipeline_db import snapshot_query
from utils.service_linking import (
    SNAPSHOT_TABLES,
    LlmServiceResolver,
    build_plan,
    build_plan_from_rows,
    dump_plan,
    item_unit_type,
    recognize_deterministic,
    render_apply,
    scan_services,
)

ENUMS = list(SERVICE_NAME_ENUM_VALUES)


def offer(offer_id=1, text="Botox special", promotion_id=10, membership_plan_id=None, **extra):
    return {"id": offer_id, "business_id": 7, "offer_raw_text": text, "promotion_id": promotion_id,
            "membership_plan_id": membership_plan_id, "is_package": False, "discount_price": None, **extra}


def item(item_id, offer_id=1, name="Botox", service_id=None, **extra):
    return {"offer_item_id": item_id, "offer_id": offer_id, "item_name": name, "service_id": service_id,
            "delivered_units": None, "unit_price": None, **extra}


def service(service_id, name, unit_type=None, area=None):
    return {"service_id": service_id, "business_id": 7, "service_name": name, "unit_type": unit_type,
            "service_area": area}


PROMOTION = {"promotion_id": 10, "source_url": "https://clinic.test/specials/", "is_offer_extracted": False}


def plan(items, offers=None, services=None, promotions=None, memberships=None, resolver=None):
    return build_plan_from_rows(
        items=items, offers=offers or [offer()], services=services or [],
        promotions=[PROMOTION] if promotions is None else promotions, memberships=memberships or [],
        enum_values=ENUMS, resolver=resolver)


class FakeClient:
    def __init__(self, answers):
        self.answers = answers
        self.calls = 0

    def create_json_response(self, messages, *, json_schema=None):
        self.calls += 1
        return {"results": [{"index": i, "service_name": name} for i, name in self.answers.items()]}


def test_deterministic_recognition_from_item_name_and_alias():
    assert scan_services("20 units of Dysport") == {"Dysport"}
    assert scan_services("Juvederm lip filler") == {"Juvéderm"}
    assert scan_services("Semaglutide weekly") == {"Weight Loss"}
    result = recognize_deterministic(item_name="Hydrafacial", offer_raw_text="", quantity=None,
                                     sibling_count=1, is_package=False)
    assert result["service_name"] == "Hydrafacial" and result["basis"] == "deterministic"


def test_multiple_services_in_item_is_unresolved_without_llm():
    result = recognize_deterministic(item_name="Botox and Dermal Filler combo", offer_raw_text="", quantity=None,
                                     sibling_count=1, is_package=False)
    assert result["basis"] == "unresolved" and result["reason"] == "multiple_services_in_item"


def test_unit_type_only_when_confirmed():
    assert item_unit_type("Botox 20 units") == "unit"
    assert item_unit_type("Filler syringe") == "syringe"
    assert item_unit_type("Botox units or vial") is None
    assert item_unit_type("Botox") is None


def test_blanket_offer_keeps_null():
    result = plan([item(1, name="Botox")], offers=[offer(text="20% off all services")])
    assert result["items"][0]["action"] == "unresolved" and result["items"][0]["reason"] == "blanket_offer"
    assert result["promotions_to_mark"] == []


def test_existing_service_is_linked():
    result = plan([item(1)], services=[service(100, "Botox")])
    entry = result["items"][0]
    assert (entry["action"], entry["service_id"], entry["basis"]) == ("link", 100, "deterministic")
    assert result["services_to_create"] == []
    assert result["promotions_to_mark"] == [10]


def test_missing_service_is_created_with_null_regular_price():
    result = plan([item(1)])
    entry = result["items"][0]
    assert entry["action"] == "create_and_link"
    (spec,) = result["services_to_create"]
    assert spec["regular_price"] is None and spec["service_name"] == "Botox" and spec["unit_type"] is None
    assert spec["source_url"] == "https://clinic.test/specials" and entry["create_key"] == spec["key"]


def test_multiple_service_rows_match_by_unit_type_else_unresolved():
    rows = [service(100, "Botox", "unit"), service(101, "Botox", "syringe")]
    ok = plan([item(1, name="Botox 20 units")], services=rows)
    assert ok["items"][0]["service_id"] == 100
    ambiguous = plan([item(1, name="Botox")], services=rows)
    assert ambiguous["items"][0]["action"] == "unresolved"
    assert ambiguous["items"][0]["reason"] == "multiple_service_rows"
    assert ambiguous["promotions_to_mark"] == []


def test_existing_valid_service_id_untouched_and_duplicate_in_offer_unresolved():
    items = [item(1, service_id=100), item(2, name="Botox 20 units")]
    result = plan(items, services=[service(100, "Botox")])
    assert [e["offer_item_id"] for e in result["items"]] == [2]
    assert result["items"][0]["reason"] == "duplicate_service_in_offer"


def test_is_offer_extracted_requires_all_non_blanket_items_linked():
    offers = [offer(1, "Botox special"), offer(2, "Spring offer"), offer(3, "10% off gift cards")]
    items = [item(1, 1, "Botox"), item(2, 2, "Mystery treatment"), item(3, 3, "Gift card")]
    result = plan(items, offers=offers, resolver=LlmServiceResolver(FakeClient({2: "NONE"}), ENUMS))
    assert result["promotions_to_mark"] == []
    only_blanket_free = plan([item(1, 1, "Botox"), item(3, 3, "Gift card")], offers=[offers[0], offers[2]])
    assert only_blanket_free["promotions_to_mark"] == [10]
    already = plan([item(1)], promotions=[{**PROMOTION, "is_offer_extracted": True}])
    assert already["promotions_to_mark"] == []


def test_membership_marked_when_complete():
    member = {"plan_id": 5, "source_url": "https://clinic.test/membership", "is_offer_extracted": False}
    result = plan([item(1)], offers=[offer(promotion_id=None, membership_plan_id=5)], promotions=[],
                  memberships=[member])
    assert result["memberships_to_mark"] == [5]
    assert result["services_to_create"][0]["source_url"] == "https://clinic.test/membership"


def test_llm_resolves_only_when_deterministic_fails():
    client = FakeClient({1: "Weight Loss"})
    resolver = LlmServiceResolver(client, ENUMS)
    result = plan([item(1, name="Mystery vial")], offers=[offer(text="Spring offer")], resolver=resolver)
    assert result["items"][0]["basis"] == "llm" and result["items"][0]["recognized"] == "Weight Loss"
    assert client.calls == 1
    deterministic = plan([item(1)], resolver=resolver)
    assert deterministic["items"][0]["basis"] == "deterministic" and client.calls == 1


def test_llm_none_and_multiple_are_unresolved():
    resolver = LlmServiceResolver(FakeClient({1: "NONE", 2: "MULTIPLE"}), ENUMS)
    offers = [offer(1, "Spring offer"), offer(2, "Summer offer")]
    result = plan([item(1, 1, "Mystery"), item(2, 2, "Another mystery")], offers=offers, resolver=resolver)
    assert [e["reason"] for e in result["items"]] == ["llm_none", "llm_multiple_services"]


def test_llm_needed_without_resolver_fails_fast():
    with pytest.raises(RuntimeError, match="llm_required"):
        plan([item(1, name="Mystery")], offers=[offer(text="Spring offer")])


def test_price_invariant_violation_is_unresolved():
    broken = offer(discount_price=99)
    result = plan([item(1)], offers=[broken])
    assert result["items"][0]["reason"] == "price_invariant_would_fail"


def sql_for(frozen):
    return render_apply(json.loads(dump_plan(frozen)))


def test_apply_sql_creates_service_links_item_and_marks_promotion():
    text = sql_for(plan([item(1)]))
    assert text.count("do $pipeline$") == 1 and text.rstrip().endswith("$pipeline$;")
    assert "v_service_0 bigint" in text
    assert "returning service_id into v_service_0" in text
    assert "cardinality(v_ids) > 1" in text and "stale_plan: business 7 now has several Botox rows" in text
    assert "values (7, 'Botox'::clinic_service_name_enum, NULL, NULL, 'https://clinic.test/specials', now())" in text
    link = next(line for line in text.splitlines() if "update public.promo_offer_items" in line)
    assert "set service_id = v_service_0" in link and "i.service_id is null" in link
    assert "get diagnostics v_count = row_count;" in text
    assert "if v_count <> 1 then raise exception 'stale_plan: item 1 cannot be linked" in text
    assert text.index("returning service_id into v_service_0") < text.index("update public.promo_offer_items")
    assert "update public.clinic_promotions set is_offer_extracted = true" in text
    assert "where promotion_id = 10;" in text
    assert "m.promotion_id = 10 and i.service_id is null" in text
    assert "if v_count <> 0 then raise exception 'incomplete_mapping: clinic_promotions 10" in text


def test_apply_sql_links_existing_service_without_creating():
    text = sql_for(plan([item(1)], services=[service(100, "Botox")]))
    assert "insert into public.clinic_services" not in text and "v_service_0" not in text
    assert "set service_id = 100" in text and "s.service_id = 100" in text


def test_apply_sql_reuses_one_variable_per_create_key():
    offers = [offer(1, "Botox special"), offer(2, "More Botox")]
    text = sql_for(plan([item(1, 1, "Botox"), item(2, 2, "Botox")], offers=offers))
    assert text.count("insert into public.clinic_services") == 1
    assert text.count("set service_id = v_service_0") == 2


def test_apply_sql_runs_blanket_check_on_batch_items_only():
    text = sql_for(plan([item(1)], services=[service(100, "Botox")]))
    assert "(all services|any service|all products|all treatments|gift card|skin ?care products)'" in text
    assert "and i.offer_item_id = any('{1}'::int2[]::bigint[])) blanket_rows;" in text
    assert "raise exception 'blanket_check_failed" in text
    assert text.index("update public.clinic_promotions") < text.index("blanket_rows")


def test_apply_sql_unlinked_check_uses_blanket_regex_and_handles_null_text():
    text = sql_for(plan([item(1)], services=[service(100, "Botox")]))
    assert "not (coalesce(m.offer_raw_text, '') ~* " in text
    assert "all services|any service|all products|all treatments|gift card|skin ?care products" in text
    assert "entire menu" in text


def test_apply_sql_marks_membership_by_plan_id():
    member = {"plan_id": 5, "source_url": "https://clinic.test/membership", "is_offer_extracted": False}
    frozen = plan([item(1)], offers=[offer(promotion_id=None, membership_plan_id=5)], promotions=[],
                  memberships=[member])
    text = sql_for(frozen)
    assert "m.membership_plan_id = 5 and i.service_id is null" in text
    assert "update public.clinic_memberships set is_offer_extracted = true, updated_at = now() where plan_id = 5;" in text


def test_apply_sql_without_links_still_checks_batch():
    text = sql_for(plan([item(1, name="Botox")], offers=[offer(text="20% off all services")]))
    assert "update public.promo_offer_items" not in text and "update public.clinic_promotions" not in text
    assert "blanket_rows" in text


def test_build_plan_from_snapshot_orders_enum_values():
    snapshot = {"items": [item(1)], "offers": [offer()], "services": [service(100, "Botox")],
                "promotions": [PROMOTION], "memberships": [],
                "enums": [{"enumlabel": name, "enumsortorder": index} for index, name in reversed(list(enumerate(ENUMS)))]}
    result = build_plan(snapshot, None)
    assert result["items"][0]["service_id"] == 100
    snapshot["enums"] = snapshot["enums"][1:]
    with pytest.raises(RuntimeError, match="enum_mismatch"):
        build_plan(snapshot, None)


def test_snapshot_query_reads_all_tables_and_enum():
    text = snapshot_query(SNAPSHOT_TABLES)
    for name in ("items", "offers", "services", "promotions", "memberships", "enums"):
        assert f"'{name}'" in text
    assert "pg_catalog.pg_enum e join pg_catalog.pg_type t" in text and "clinic_service_name_enum" in text
    assert "public.promo_offer_items" in text and "public.clinic_memberships" in text
