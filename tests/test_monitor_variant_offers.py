from pathlib import Path

from utils.monitor_offer_tiers import merge_member_tiers, merge_new_customer_tiers, visible_variant_offers


def test_compare_at_stays_regular_and_both_sales_remain(tmp_path):
    shot = tmp_path / "v.png"
    shot.write_bytes(b"\x89PNG\r\n\x1a\n")
    browser = {
        "variant_captures": [
            {"purchasable": True, "url": "https://shop.example/products/tox", "variant_id": "1",
             "selected_options": ["Non-Member", "10 Units TOX Package"], "title": "Naked TOX",
             "discount_price": 79.9, "regular_price": 89.9, "offer_raw_text": "x",
             "screenshot_path": str(shot)},
            {"purchasable": True, "url": "https://shop.example/products/tox", "variant_id": "2",
             "selected_options": ["Member", "10 Units TOX Package"], "title": "Naked TOX",
             "discount_price": 69.9, "regular_price": 89.9, "offer_raw_text": "y",
             "screenshot_path": str(shot)},
        ]
    }
    offers = visible_variant_offers(browser)
    assert [(o["is_membership_required"], o["regular_price"], o["discount_price"]) for o in offers] == [
        (False, 89.9, 79.9), (True, 89.9, 69.9)]
    assert offers[1]["items"][0]["delivered_units"] == 10


def test_two_tier_variant_still_folds_non_member_into_regular(tmp_path):
    shot = tmp_path / "v.png"
    shot.write_bytes(b"\x89PNG\r\n\x1a\n")
    browser = {
        "variant_captures": [
            {"purchasable": True, "url": "https://shop.example/products/tox", "variant_id": "1",
             "selected_options": ["Non-Member", "10 Units"], "title": "TOX",
             "discount_price": 10.0, "regular_price": None, "offer_raw_text": "non",
             "screenshot_path": str(shot)},
            {"purchasable": True, "url": "https://shop.example/products/tox", "variant_id": "2",
             "selected_options": ["Member", "10 Units"], "title": "TOX",
             "discount_price": 9.0, "regular_price": None, "offer_raw_text": "member",
             "screenshot_path": str(shot)},
        ]
    }
    offers = visible_variant_offers(browser)
    assert len(offers) == 1
    assert offers[0]["is_membership_required"] is True
    assert offers[0]["regular_price"] == 10.0
    assert offers[0]["discount_price"] == 9.0


def test_nakedmd_audit_rules_fire_on_member_regular_and_units():
    from datetime import datetime, timezone
    from utils.data_quality_audit import audit_snapshot

    url = "https://www.nakedmd.com/collections/specials"
    g = {"promotions": [{"promotion_id": 58, "business_id": 2986, "source_url": url, "is_active": True,
                         "campaign_end_date": None, "promotion_content": [
                             "Naked TOX non-member 10 units $79.90; member $69.90"]}],
         "memberships": [], "services": [], "offers": [], "items": []}
    g["offers"].append({"id": 1, "business_id": 2986, "promotion_id": 58, "membership_plan_id": None,
                        "is_active": True, "price_model": "total", "is_package": False,
                        "is_membership_required": True, "is_new_customer_required": False,
                        "conditions": ["Member", "10 Units TOX Package"],
                        "discount_price": 69.9, "regular_price": 79.9,
                        "offer_raw_text": "Naked TOX Member 10 units $69.90 (regular $89.90)"})
    g["items"].append({"offer_item_id": 10, "offer_id": 1, "item_name": "Member 10 Units TOX",
                       "service_id": None, "delivered_units": 1, "unit_price": 69.9})
    at = datetime(2026, 10, 20, tzinfo=timezone.utc)
    rules = {i["rule"] for i in audit_snapshot(g, at=at)["issues"]}
    assert "G_units_in_text_not_delivered" in rules
    assert "H_member_nonmember_regular" in rules  # stored 79.90 is the non-member sale, not compare-at 89.90
    g["offers"][0]["regular_price"] = 89.9
    rules = {i["rule"] for i in audit_snapshot(g, at=at)["issues"]}
    assert "H_member_nonmember_regular" not in rules
    g["offers"][0]["regular_price"] = 79.9
    g["offers"][0]["offer_raw_text"] = "Naked TOX Member 10 units $69.90 (non-member $79.90)"
    rules = {i["rule"] for i in audit_snapshot(g, at=at)["issues"]}
    assert "H_member_nonmember_regular" not in rules
    g["offers"][0]["regular_price"] = 69.9
    rules = {i["rule"] for i in audit_snapshot(g, at=at)["issues"]}
    assert "H_member_nonmember_regular" in rules


def _text_offer(text, member, price, item="Jeuveau", model="per_unit"):
    return {"offer_raw_text": text, "is_membership_required": member, "discount_price": price,
            "regular_price": None, "price_model": model, "items": [{"item_name": item, "quantity": None}]}


def test_new_customer_tiers_fold_returning_price_into_regular():
    offers = [
        {**_text_offer("First Tox Jeuveau $8/unit", False, 8.0, "Jeuveau", "per_unit"),
         "is_new_customer_required": True},
        {**_text_offer("Returning Patients Jeuveau $9/unit", False, 9.0, "Jeuveau", "per_unit"),
         "is_new_customer_required": False},
    ]
    merged = merge_new_customer_tiers(offers)
    assert len(merged) == 1
    assert merged[0]["discount_price"] == 8.0
    assert merged[0]["regular_price"] == 9.0


def test_text_member_tiers_fold_non_member_price_into_regular():
    offers = [_text_offer("Vault Members Jeuveau $9/unit", True, 9.0),
              _text_offer("Non Members Jeuveau $10/unit", False, 10.0),
              _text_offer("Trio; Black Card members $1,582 (save $1,055)", True, 1582.0, "Filler", "total"),
              _text_offer("Trio; Luxe Club members $1,670 (save $967)", True, 1670.0, "Filler", "total"),
              _text_offer("Trio; non-members $1,758 (save $879)", False, 1758.0, "Filler", "total")]
    merged = merge_member_tiers(offers)
    assert [(o["offer_raw_text"][:5], o["regular_price"]) for o in merged] == [
        ("Vault", 10.0), ("Trio;", 1758.0), ("Trio;", 1758.0)]


def test_compare_at_member_tier_keeps_the_non_member_offer():
    member = _text_offer("Member 10 units $69.90 regular price $89.90", True, 69.9)
    member["regular_price"] = 89.9
    sibling = _text_offer("Non-member 10 units $79.90", False, 79.9)
    merged = merge_member_tiers([member, sibling])
    assert [(o["is_membership_required"], o["regular_price"], o["discount_price"]) for o in merged] == [
        (True, 89.9, 69.9), (False, 89.9, 79.9)]


def test_ambiguous_or_different_item_non_member_offers_are_kept():
    ambiguous = [_text_offer("Members Botox $9/unit", True, 9.0, "Botox"),
                 _text_offer("Non-members Botox $11/unit", False, 11.0, "Botox"),
                 _text_offer("Non-members Botox weekday $10/unit", False, 10.0, "Botox")]
    assert merge_member_tiers(ambiguous) == ambiguous
    different = [_text_offer("Members Botox $9/unit", True, 9.0, "Botox"),
                 _text_offer("Non-members Dysport $5/unit", False, 5.0, "Dysport")]
    assert merge_member_tiers(different) == different
