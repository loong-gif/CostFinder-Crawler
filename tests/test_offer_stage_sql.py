from __future__ import annotations

import json

from offer_stage_support import NOW, existing_item, existing_offer, llm_offer, promo_row

from utils import offer_stage
from utils.offer_stage_reconcile import plan_source
from utils.offer_stage_spec import BLANKET_CHECK_SQL, PROMOTIONS, source_evidence
from utils.offer_stage_sql import render_apply


def ready_plan():
    raw = "Botox special $10/unit"
    offers = [existing_offer(100, raw), existing_offer(101, "Dysport $8/unit")]
    items = {100: [existing_item(500, 100, service_id=55)], 101: [existing_item(501, 101)]}
    plan_row = promo_row([raw, "Dysport special just $90"])
    payload = {"offers": [llm_offer(raw), llm_offer("Dysport special just $90", items=[
        {"item_name": "Dysport", "service_name": "Dysport", "quantity": None, "unit_price": 90, "unit_type": None,
         "service_area": None}], discount_price=90, price_model="total")]}
    source = plan_source(PROMOTIONS, plan_row, payload, source_evidence(PROMOTIONS, plan_row), offers, items, {55})
    return {"sources": [source]}, plan_row, offers, items


def rendered():
    plan, *_ = ready_plan()
    return render_apply(json.loads(offer_stage.dump_plan(plan)))


def test_apply_sql_is_single_do_block_with_freshness_checks_first():
    text = rendered()
    assert text.count("do $pipeline$") == 1 and text.rstrip().endswith("$pipeline$;")
    assert f"'{NOW.isoformat()}'::timestamptz" in text
    first_write = min(text.index(w) for w in ("update public.promo_offer_master", "insert into public.promo_offer_master"))
    for check in ("stale_plan: clinic_promotions 7", "stale_plan: offers of", "stale_plan: offers [100, 101] of",
                  "stale_plan: items of the offers of"):
        assert text.index(check) < first_write
    assert "for update;" in text and "promotion_id = 7" in text
    # 源行与 offer 的 id/updated_at、item 归属各用一条语句核对
    assert "array_agg(updated_at order by id)" in text and "::timestamptz[]" in text
    assert "array_agg(offer_id || ':' || offer_item_id order by offer_id, offer_item_id)" in text
    assert "'{100:500,101:501}'::text[]" in text


def test_apply_sql_deactivates_before_insert_then_activates_after_items():
    text = rendered()
    deactivate = text.index("set is_active=false, updated_at=now() where id=101 and is_active")
    insert = text.index("insert into public.promo_offer_master")
    item = text.index("insert into public.promo_offer_items")
    activate = text.index("set is_active=true, updated_at=now() where id=v_offer")
    assert deactivate < insert < item < activate
    assert "false) returning id into v_offer;" in text and "v_new := v_new || v_offer;" in text
    assert "values (v_offer, 'Dysport'" in text
    assert "delete from public.promo_offer_master" not in text


def test_apply_sql_verifies_activation_after_writes():
    text = rendered()
    check = text.index("offer_activation_changed_by_database: clinic_promotions 7")
    assert check > text.index("set is_active=true")
    assert "and is_active) <> cardinality((v_new || '{}'::bigint[]))" in text
    assert "and not is_active) <> 1" in text


def test_apply_sql_runs_blanket_check_on_touched_offers_last():
    text = rendered()
    assert " ".join(BLANKET_CHECK_SQL.split()) in " ".join(text.split())
    assert "and m.id = any(v_touched)" in text and "raise exception 'blanket_check_failed: %', v_violations" in text
    assert text.index("v_touched := v_touched ||") < text.index("blanket_check_failed")


def test_apply_sql_leaves_is_offer_extracted_to_the_trigger():
    assert "is_offer_extracted" not in rendered()


def test_branch_sources_with_identical_new_offers_are_written_in_one_loop():
    raw = "40% off IPL Photofacial"
    payload = {"offers": [llm_offer(raw, price_model="total", discount_price=None, discount_percent=40,
                                    items=[{"item_name": "IPL Photofacial", "service_name": "IPL Photofacial",
                                            "quantity": None, "unit_price": None, "unit_type": None,
                                            "service_area": None}])]}
    sources = []
    for promotion_id, business_id in ((7, 3), (8, 4), (9, 5)):
        row = {**promo_row([raw], promotion_id=promotion_id), "business_id": business_id}
        sources.append(plan_source(PROMOTIONS, row, payload, source_evidence(PROMOTIONS, row), [], {}, set()))
    # 第四个来源已有 offer，不参与合并，仍逐源写入
    other = promo_row([raw], promotion_id=10)
    sources.append(plan_source(PROMOTIONS, other, payload, source_evidence(PROMOTIONS, other),
                               [existing_offer(100, raw, promotion_id=10)], {100: []}, set()))
    text = render_apply(json.loads(offer_stage.dump_plan({"sources": sources})))
    loop = text[text.index("for v_row in select value from jsonb_array_elements("):text.index("end loop;")]
    for state in ('{"id": 7, "business": 3,', '{"id": 8, "business": 4,', '{"id": 9, "business": 5,'):
        assert state in loop
    assert loop.count("insert into public.promo_offer_master") == 1 and "v_business, v_source" in loop
    assert "stale_plan: % % changed since planning" in loop and "(v_row->>'updated_at')::timestamptz" in loop
    assert loop.index("for update;") < loop.index("insert into public.promo_offer_master")
    assert "promotion_id = 10" in text[text.index("end loop;"):]
    assert text.count("insert into public.promo_offer_master") == 1

    # 名下已有不变 offer、只新增同一条 offer 的分店同样合并：循环内核对各自已有 offer 的 id、updated_at 与 item
    extra = "Buy 2 Hydrafacials for $299"
    payload2 = {"offers": payload["offers"] + [llm_offer(extra, price_model="total", discount_price=299, items=[
        {"item_name": "Hydrafacial", "service_name": "Hydrafacial", "quantity": 2, "unit_price": None,
         "unit_type": None, "service_area": None}])]}
    grown = []
    for promotion_id, business_id, offer_id in ((7, 3, 100), (8, 4, 101)):
        row = {**promo_row([raw, extra], promotion_id=promotion_id), "business_id": business_id}
        first = sources[0]["offers"][0]["master"]
        existing = existing_offer(offer_id, raw, promotion_id=promotion_id, business_id=business_id,
                                  **{k: first[k] for k in ("price_model", "discount_price", "discount_percent",
                                                          "conditions", "is_new_customer_required", "is_package")})
        grown.append(plan_source(PROMOTIONS, row, payload2, source_evidence(PROMOTIONS, row), [existing],
                                 {offer_id: [existing_item(500 + offer_id, offer_id, name="IPL Photofacial",
                                                           price=None)]}, set()))
    assert [[a["action"] for a in s["offers"]] for s in grown] == [["unchanged", "insert"], ["unchanged", "insert"]]
    text = render_apply(json.loads(offer_stage.dump_plan({"sources": grown})))
    assert text.count("insert into public.promo_offer_master") == 1
    assert '"offers": [100]' in text and '"items": ["100:600"]' in text and '"offers": [101]' in text


def test_apply_sql_skips_sources_without_offer_actions():
    plan, *_ = ready_plan()
    plan["sources"][0]["offers"] = []
    text = render_apply(plan)
    assert "insert into public.promo_offer_master" not in text and "stale_plan" not in text
    assert "blanket_check_failed" in text
