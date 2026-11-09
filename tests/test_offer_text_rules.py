from utils.offer_scope_filter import item_name_problem, non_promotable_reason
from utils.offer_stage_extract import _merge_restated_offers, candidates_from_payload


def candidate(raw, item_names=("Item",)):
    return {"offer_raw_text": raw, "items": [{"item_name": name} for name in item_names]}


def test_flattened_fragments_are_not_promotable():
    for raw, names in (
        ("10% off | and already discounted packages", ("and already discounted packages",)),
        ("15% off | and lab work (no limits | ; must be ordered / performed at Renew)", ("and lab work (no limits",)),
        ("15% off | peptides | ; must be ordered / performed at Renew)", ("peptides",)),
    ):
        assert non_promotable_reason(candidate(raw, names)) == "fragment_offer_text"


def test_membership_term_gift_card_and_event_discounts_are_not_promotable():
    assert non_promotable_reason(candidate(
        "Purchase 12 months upfront and receive 3 additional months free (20% savings)", ("Membership",))
    ) == "membership_term_offer"
    assert non_promotable_reason(candidate(
        "Buy one Ultra Smooth Skin Gift card for $75 towards Botox, Get $75 FREE")) == "gift_card_offer"
    assert non_promotable_reason(candidate("15% off | member events | .", ("member events",))) == "non_service_target"


def test_item_names_about_members_events_or_gift_cards_are_not_promotable():
    for name in ("member events", "Member 10 Units TOX", "Non-Member 50 Units TOX", "VIP Membership",
                 "10% off all services during Membership period", "Core Members 20% off",
                 "VIP event invitations, member-only offers and 15% off skincare/products.",
                 "Holiday gift cards as the perfect stocking stuffer"):
        assert non_promotable_reason(candidate("15% off services", (name,))) == "non_service_target", name


def test_clause_fragments_cut_from_terms_are_not_item_names():
    for name in ("a package of 8", "a small area of the face", "JEUVEAU is available"):
        assert item_name_problem(name) == "clause_fragment", name
    for name in ("Laser Hair Removal Treatments (Small Area)", "Botox", "Acne Facial", "A E Skin Signature Facial"):
        assert item_name_problem(name) == "", name


def test_terms_paragraph_is_not_offer_text():
    raw = ("The Small Area Laser Hair Removal Package Promotion ǂValid on purchases made November 3, 2026 to "
           "December 7, 2026 by phone or in person. Receive 75% off a package of 8 or 10 laser hair removal treatments")
    assert non_promotable_reason(candidate(raw, ("Laser Hair Removal",))) == "terms_paragraph_offer_text"


def test_offer_text_taken_from_the_terms_paragraph_moves_to_the_card_headline():
    terms = ("The Small Area Laser Hair Removal Package Promotion ǂValid on purchases made November 3, 2026 to "
             "December 7, 2026 by phone or in person. Receive 75% off a package of 8 or 10 laser hair removal "
             "treatments on a small area of the body. Limit 1 per patient.")
    card = f"The Black Friday Sale\n75% OFF LASER HAIR REMOVAL\nSelect packages.\n{terms}"
    row = {"promotion_id": 7, "business_id": 1, "source_url": "https://glow.example/bundles", "promotion_content": [card]}
    offer = {"offer_raw_text": terms, "price_model": "total", "regular_price": None, "discount_price": None,
             "discount_percent": 75, "discount_amount": None, "is_package": True, "conditions": [],
             "is_new_customer_required": False, "is_membership_required": False,
             "items": [{"item_name": "a package of 8", "service_name": "Laser Hair Removal"}]}
    candidates, rejected = candidates_from_payload({"offers": [offer]}, "clinic_promotions", row, card)
    assert [c["master"]["offer_raw_text"] for c in candidates] == ["75% OFF LASER HAIR REMOVAL"], rejected
    assert candidates[0]["master"]["discount_percent"] == 75


def test_restated_offer_in_the_same_source_keeps_the_copy_with_more_conditions():
    def offer(raw, conditions):
        master = {"offer_raw_text": raw, "conditions": conditions, "price_model": "total", "regular_price": None,
                  "discount_price": None, "discount_percent": None, "discount_amount": 50.0, "is_package": False,
                  "is_new_customer_required": True, "is_membership_required": False}
        return {"master": master, "items": [{"item_name": "BOTOX® Cosmetic", "delivered_units": None, "unit_price": None}]}

    rejected = []
    kept = _merge_restated_offers(
        [offer("For a limited time, $50 off BOTOX for new patients", []),
         offer("For new patients, get $50 off BOTOX", ["Must purchase a minimum of 40 units."])],
        lambda text, reason: rejected.append(reason))
    assert [k["master"]["conditions"] for k in kept] == [["Must purchase a minimum of 40 units."]]
    assert rejected == ["duplicate_in_source"]


def test_ordinary_service_discounts_stay_promotable():
    for raw, names in (
        ("10% off | treatments", ("treatments",)),
        ("10% off | services (including injectables | ; excluding neurotoxins)", ("services (including injectables",)),
        ("Receive 10 percent off most products and services (excludes packages, already discounted items)", ("x",)),
        ("15% off | additional aesthetic services | .", ("additional aesthetic services",)),
        ("2) $65 off your first BOTOX® Cosmetic treatment", ("BOTOX® Cosmetic",)),
    ):
        assert non_promotable_reason(candidate(raw, names)) == ""
