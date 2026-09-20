from utils.membership_offer_items import classify_membership_offer_scope, expand_membership_offer_services


def services():
    return [
        {"service_id": 1, "service_name": "Botox", "service_category": "Neurotoxin"},
        {"service_id": 2, "service_name": "Dermal Filler", "service_category": "Filler"},
    ]


def test_classifies_product_before_service_scope():
    assert classify_membership_offer_scope("10% off all skincare products") == "product"


def test_expands_all_services():
    scope, rows, reason = expand_membership_offer_services("10% off all services", services())
    assert (scope, [row["service_id"] for row in rows], reason) == ("all_services", [1, 2], None)


def test_maps_unique_service_and_rejects_ambiguous():
    assert expand_membership_offer_services("$50 off filler", services())[1][0]["service_id"] == 2
    scope, rows, reason = expand_membership_offer_services("10% off treatments", services())
    assert scope == "service" and rows == [] and reason == "service_mapping_ambiguous_or_missing"
