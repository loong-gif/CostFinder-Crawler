from utils.promotion_content_cards import (
    bind_promotion_content_segments,
    extract_offer_cards_from_markdown,
    validate_promotion_content_binding,
)
from utils.promotion_source_page import (
    PAGE_TYPE_BLOG,
    PAGE_TYPE_GENERIC,
    PAGE_TYPE_MEMBERSHIP,
    PAGE_TYPE_SPECIAL,
    classify_promotion_source_page,
    gate_promotion_source_page,
)


def test_covid_blog_is_not_promotion_source() -> None:
    classification = classify_promotion_source_page(
        url="https://newportderm.com/blog/covid-vaccine-clinic-update",
        title="COVID Vaccine Update | Newport Beach Dermatology",
        markdown=(
            "Posted on January 12. Share this article. "
            "Related articles about vaccine scheduling."
        ),
    )
    assert classification.page_type == PAGE_TYPE_BLOG
    gate = gate_promotion_source_page(
        url="https://newportderm.com/blog/covid-vaccine-clinic-update",
        title=classification.reason,
        markdown="COVID-19 vaccine information for patients.",
    )
    assert not gate.accepted
    assert "ineligible_page_type:blog" in gate.reason


def test_membership_page_is_not_promotion_source() -> None:
    classification = classify_promotion_source_page(
        url="https://tievclinic.com/membership",
        title="Membership Plans",
        markdown="Vitality membership $149/month. Join our members club today.",
    )
    assert classification.page_type == PAGE_TYPE_MEMBERSHIP
    gate = gate_promotion_source_page(
        url="https://tievclinic.com/membership",
        title="Membership Plans",
        markdown="Vitality membership $149/month.",
    )
    assert not gate.accepted
    assert "ineligible_page_type:membership" in gate.reason


def test_vip_membership_page_is_not_promotion_source() -> None:
    classification = classify_promotion_source_page(
        url="https://somaaesthetics.com/vip-beauty-bank",
        title="VIP Beauty Bank Membership",
        markdown="VIP Beauty Bank $199/month with member perks.",
    )
    assert classification.page_type == PAGE_TYPE_MEMBERSHIP


def test_generic_contact_cart_page_is_not_promotion_source() -> None:
    classification = classify_promotion_source_page(
        url="https://bmedspa.com/occasion",
        title='{"title":"Occasion Intro"}',
        markdown="Welcome. Contact us to learn more. Subtotal $0.00",
    )
    assert classification.page_type == PAGE_TYPE_GENERIC
    assert not gate_promotion_source_page(
        url="https://bmedspa.com/occasion",
        title='{"title":"Occasion Intro"}',
        markdown="Contact us. Subtotal $0.00",
    ).accepted


def test_specials_page_remains_eligible() -> None:
    gate = gate_promotion_source_page(
        url="https://clinic.example/specials",
        title="July Specials",
        markdown="Botox $11/unit. Save 20% on Juvederm filler.",
    )
    assert gate.accepted
    assert classify_promotion_source_page(
        url="https://clinic.example/specials",
        title="July Specials",
        markdown="Botox $11/unit.",
    ).page_type == PAGE_TYPE_SPECIAL


def test_nakedmd_style_fragments_bind_into_cards() -> None:
    evidence = (
        "## Monthly Specials\n\n"
        "![Botox $11/unit NakedMD](https://cdn.example/botox.png)\n\n"
        "Regular $14/unit\n\n"
        "Save 20%\n\n"
        "![Juvederm Ultra $599](https://cdn.example/juv.png)\n\n"
        "Regular $750\n\n"
        "20 units minimum\n"
    )
    fragments = [
        "Botox $11/unit",
        "Regular $14/unit",
        "Save 20%",
        "Juvederm Ultra $599",
        "Regular $750",
        "20 units minimum",
    ]
    cards = extract_offer_cards_from_markdown(evidence)
    assert len(cards) <= 2
    bound = bind_promotion_content_segments(fragments, evidence)
    assert len(bound) <= 2
    assert validate_promotion_content_binding(bound).accepted
    assert not validate_promotion_content_binding(fragments).accepted


def test_bound_card_keeps_service_price_and_discount_together() -> None:
    bound = bind_promotion_content_segments(
        ["Botox $11/unit", "Regular $14/unit", "Save 20%"],
        "Botox $11/unit\nRegular $14/unit\nSave 20%",
    )
    assert len(bound) == 1
    assert "Botox" in bound[0]
    assert "$11" in bound[0]
    assert "20%" in bound[0]
