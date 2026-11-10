from copy import deepcopy
from datetime import datetime, timezone

from utils.data_quality_audit import audit_snapshot, reextract_sources, relink_items
from scripts.run_data_quality_audit import merge_queue
from utils.supabase_rest import SupabaseRestClient
import pytest

AT = datetime(2026, 10, 14, 8, tzinfo=timezone.utc)
URL = "https://clinic.example/specials"


def graph():
    return {"promotions": [{"promotion_id": 1, "business_id": 7, "source_url": URL, "is_active": True,
                             "campaign_end_date": None, "promotion_content": []}],
            "memberships": [], "services": [{"service_id": 12, "business_id": 7, "service_name": "Botox",
                "service_area": "face", "source_url": "https://clinic.example/services"}],
            "offers": [], "items": []}


def offer(g, ident=1, **kwargs):
    row = {"id": ident, "business_id": 7, "promotion_id": 1, "membership_plan_id": None,
           "is_active": True, "price_model": "total", "is_package": False,
           "is_membership_required": False, "is_new_customer_required": False, "conditions": [],
           "discount_price": 99, "regular_price": 200, "discount_amount": 101, "discount_percent": 50.5,
           "offer_raw_text": "Botox special $99 (regular $200)", **kwargs}
    g["offers"].append(row)
    g["items"].append({"offer_item_id": ident + 100, "offer_id": ident, "item_name": "Botox",
                       "service_id": 12, "delivered_units": None, "unit_price": 99})
    return row


def capture(url=URL, **kwargs):
    return {"url": url, "captured_at": AT.isoformat(), "verified": True, "complete": True,
            "viewports": [{"name": name, "complete": True} for name in ("desktop", "tablet", "mobile")],
            "image_review_complete": True, "text": "Botox specials", **kwargs}


def test_expiry_requires_current_complete_page_and_matching_deadline():
    g = graph()
    g["promotions"][0]["campaign_end_date"] = "2026-09-30"
    offer(g)
    assert not audit_snapshot(g, at=AT)["repairs"]
    ev = capture(campaign_end_date="2026-09-30")
    patches = audit_snapshot(g, {URL: ev}, at=AT)["repairs"]
    assert {(p["table"], p["id"]) for p in patches} == {("clinic_promotions", 1), ("promo_offer_master", 1)}
    ev["viewports"][2]["complete"] = False
    assert not audit_snapshot(g, {URL: ev}, at=AT)["repairs"]
    ev["viewports"][2]["complete"] = True
    ev["campaign_end_date"] = "2026-10-31"
    assert not audit_snapshot(g, {URL: ev}, at=AT)["repairs"]


def test_inactive_parent_closes_active_child_without_reactivating_anything():
    g = graph()
    g["promotions"][0]["is_active"] = False
    offer(g)
    patches = audit_snapshot(g, at=AT)["repairs"]
    assert len(patches) == 1 and patches[0]["values"] == {"is_active": False}


def test_complete_duplicate_only_qualification_components_and_null_are_distinct():
    g = graph()
    offer(g)
    offer(g, 2)
    original = deepcopy(g)
    assert [p["id"] for p in audit_snapshot(g, at=AT)["repairs"]] == [2]
    assert g == original
    g["offers"][1]["conditions"] = ["Service must be new to the patient at ACE"]
    assert not audit_snapshot(g, at=AT)["repairs"]
    g["offers"][1]["conditions"] = []
    g["items"][1]["item_name"] = "Botox with free Forma"
    assert not audit_snapshot(g, at=AT)["repairs"]
    g["items"][1]["item_name"] = "Botox"
    g["offers"][1]["is_membership_required"] = None
    assert not audit_snapshot(g, at=AT)["repairs"]


def test_protected_duplicate_group_never_discards_an_unprotected_sibling():
    g = graph()
    offer(g)
    offer(g, 2)
    report = audit_snapshot(g, at=AT, protections={"offer_ids": [1]})
    assert not report["repairs"]


def test_blanket_copies_differing_only_by_unnamed_artifact_titles_are_duplicates():
    g = graph()
    text = "Bank Membership: 15% off all purchases."
    offer(g, offer_raw_text=text, discount_price=None, regular_price=None, discount_amount=None, discount_percent=15)
    offer(g, 2, offer_raw_text=text, discount_price=None, regular_price=None, discount_amount=None, discount_percent=15)
    g["items"][1]["item_name"] = "Dysport"
    for item in g["items"]:
        item["service_id"] = None
    g["promotions"][0]["is_offer_extracted"] = False
    assert [(p["id"], p["values"]) for p in audit_snapshot(g, at=AT)["repairs"]] == [(2, {"is_active": False})]
    # Different terms still keep them apart.
    g["offers"][1]["conditions"] = ["Minimum commitment: 12 months"]
    assert not audit_snapshot(g, at=AT)["repairs"]
    # A title the text actually names is real composition, not an artifact.
    g["offers"][1]["conditions"] = []
    g["offers"][0]["offer_raw_text"] = g["offers"][1]["offer_raw_text"] = "15% off all products, including Dysport"
    assert not audit_snapshot(g, at=AT)["repairs"]


def test_blanket_scope_unlinks_and_partial_mapping_is_not_an_error():
    g = graph()
    offer(g, offer_raw_text="20% off favorite treatments and products")
    result = audit_snapshot(g, at=AT)
    assert result["repairs"][0]["values"] == {"service_id": None}
    g["items"][0]["service_id"] = None
    g["promotions"][0]["is_offer_extracted"] = False
    assert not audit_snapshot(g, at=AT)["repairs"]
    assert not any(i["rule"] == "false_with_offer" for i in result["issues"])


def test_area_uses_catalog_source_and_partial_absence_cannot_clear():
    g = graph()
    offer(g)
    ev = capture(text="Botox specials without a body area")
    assert not audit_snapshot(g, {URL: ev}, at=AT)["repairs"]
    own = "https://clinic.example/services"
    ev["url"] = own
    ev["viewports"][1]["complete"] = False
    assert not audit_snapshot(g, {own: ev}, at=AT)["repairs"]
    ev["viewports"][1]["complete"] = True
    # Page-wide absence is still not enough without the service's own quote.
    assert not audit_snapshot(g, {own: ev}, at=AT)["repairs"]
    ev["text"] = "Botox $12/unit"
    ev["services"] = {"12": {"quote": "Botox $12/unit", "verified": True}}
    assert audit_snapshot(g, {own: ev}, at=AT)["repairs"][0]["values"] == {"service_area": None}
    ev["text"] = ev["services"]["12"]["quote"] = "Botox full face $12/unit"
    assert not audit_snapshot(g, {own: ev}, at=AT)["repairs"]


def test_new_customer_cannot_borrow_another_sales_price():
    g = graph()
    offer(g, is_new_customer_required=True, regular_price=None)
    offer(g, 2, discount_price=120)
    result = audit_snapshot(g, at=AT)
    # 原价未知存 NULL 即可：不借用其他优惠的价格，也不算问题
    assert not result["repairs"] and not any(i["rule"] == "regular_price_baseline" for i in result["issues"])


def test_F_from_price_dropped_is_routed_to_reextraction():
    g = graph()
    offer(g, offer_raw_text="Botox from $99", price_model="total")
    report = audit_snapshot(g, at=AT)
    issue = next(i for i in report["issues"] if i["rule"] == "F_from_price_dropped")
    assert issue["status"] == "reextract"
    assert reextract_sources({"entries": report["rechecks"]}) == {"promotions": [1], "memberships": []}


def test_service_wording_mismatch_is_routed_to_link_services():
    g = graph()
    offer(g, offer_raw_text="Bunny Lines $99 (regular $200)")
    g["items"][0]["item_name"] = "Bunny Lines"
    # 套餐 item 名称本身由 link-services 的确定性规则得出同一服务：无需重新判定
    offer(g, 2, offer_raw_text="Iconique Lip Reset $1,200", is_package=True)
    report = audit_snapshot(g, at=AT)
    issues = [i for i in report["issues"] if i["rule"] == "service_scope_evidence"]
    assert [(i["id"], i["status"]) for i in issues] == [(101, "relink")] and not report["repairs"]
    # relink 由 link-services 消费，不进入渲染复查队列
    assert not any("service_scope_evidence" in e["reasons"] for e in report["rechecks"])
    assert relink_items(report) == [101]


def test_gone_membership_page_deactivates_its_offers():
    g = graph()
    g["memberships"] = [{"plan_id": 5, "business_id": 7, "source_url": URL, "benefits": ["10% off"]}]
    offer(g, promotion_id=None, membership_plan_id=5)
    ev = {"url": URL, "captured_at": AT.isoformat(), "gone": True}
    repairs = audit_snapshot(g, {URL: ev}, at=AT)["repairs"]
    assert [(r["rule"], r["id"], r["values"]) for r in repairs] == [("membership_page_gone", 1, {"is_active": False})]


def test_I_itemless_inactive_stale():
    g = graph()
    g["offers"].append({"id": 9, "business_id": 7, "promotion_id": 1, "membership_plan_id": None,
                        "is_active": False, "price_model": "total", "created_at": "2026-10-01T00:00:00+00:00",
                        "offer_raw_text": "shell", "discount_price": None, "regular_price": None,
                        "is_membership_required": False, "is_new_customer_required": False, "conditions": []})
    assert any(i["rule"] == "I_itemless_offer" for i in audit_snapshot(g, at=AT)["issues"])


def test_from_minimum_quantity_and_unknown_total_output():
    g = graph()
    offer(g, offer_raw_text="Botox from $99, minimum 20 units")
    g["items"][0]["delivered_units"] = 20
    report = audit_snapshot(g, at=AT)
    assert {i["rule"] for i in report["issues"]} >= {"F_from_price_dropped", "unsupported_quantity"}
    # Malformed persisted from semantics must never become a usable rate.
    assert report["price_output"][0]["usable_unit_price"] is None


def test_membership_unknown_keeps_plan_until_verified_benefits():
    g = graph()
    g["memberships"] = [{"plan_id": 461, "business_id": 7, "source_url": URL, "benefits": [], "is_offer_extracted": False}]
    assert not audit_snapshot(g, at=AT)["repairs"]
    ev = capture(memberships={"461": {"verified": True, "benefits": ["10% off products"]}})
    result = audit_snapshot(g, {URL: ev}, at=AT)
    assert result["repairs"][0]["values"] == {"benefits": ["10% off products"]}
    assert result["repairs"][0]["table"] == "clinic_memberships"


def test_queue_keeps_the_retry_backoff_across_daily_scans():
    entry = {"source_url": URL, "source_kind": "membership", "business_id": 7, "status": "pending", "attempts": 3,
             "retry_after": "2026-10-20T00:00:00+00:00"}
    merged = merge_queue({"entries": [entry]}, [{**entry, "attempts": 0, "retry_after": None}])
    assert (merged["entries"][0]["attempts"], merged["entries"][0]["retry_after"]) == (3, "2026-10-20T00:00:00+00:00")


@pytest.mark.parametrize("method,args", [("update_row", ("clinic_services", 1, {})),
    ("insert_rows", ("clinic_services", [{}])), ("delete_rows", ("clinic_services", {})),
    ("upsert_rows", ("clinic_services", [{}])), ("rpc", ("write_anything", {}))])
def test_rest_writes_are_rejected_before_any_request(method, args):
    # 直接写库路径一律被 forbid_direct_write 拒绝，不论项目是否匹配，且不发出任何请求。
    from utils.pipeline_run import DirectDatabaseWrite
    client = SupabaseRestClient("https://ebamlycrvgtivuluipvn.supabase.co", "unused-test-key")
    with pytest.raises(DirectDatabaseWrite):
        getattr(client, method)(*args)


def test_stated_units_ignore_thresholds_and_neighbouring_prices():
    from utils.data_quality_evidence import _stated_fixed_units
    assert _stated_fixed_units("BBL + MOXI combo 3 SESSIONS $3,000.00") == {3}
    assert _stated_fixed_units("get $40 off 20 units Jeuveau ; Minimum 20 units required.") == set()
    assert _stated_fixed_units("Special $1400 ; Treatment(s) within 6 months") == set()
    assert _stated_fixed_units("Requires purchase of 2 treatment areas ; 50+ units") == set()
