from decimal import Decimal

from utils.clinic_service_quality import evaluate_service, service_mentions


def test_price_requires_same_line_service_price_and_unit() -> None:
    decision = evaluate_service("Botox", ["Botox $11 per unit\nDysport $3.70 per unit"])
    assert decision.regular_price == Decimal("11")
    assert decision.unit_type == "unit"
    assert decision.verified is True


def test_price_for_another_service_is_not_borrowed() -> None:
    decision = evaluate_service("Dysport", ["Botox $11 per unit\nDysport treatments available"])
    assert decision.regular_price is None
    assert decision.raw_text == "Dysport treatments available"
    assert decision.verified is True


def test_filler_and_vial_units_are_not_unit() -> None:
    assert evaluate_service("Dermal Filler", ["Dermal filler $750 per syringe"]).unit_type == "syringe"
    assert evaluate_service("Sculptra", ["Sculptra $900 per vial"]).unit_type == "vial"


def test_promotion_price_is_not_a_catalogue_price() -> None:
    decision = evaluate_service("Botox", ["Botox special $9 per unit"])
    assert decision.regular_price is None
    assert decision.verified is True


def test_generic_filler_does_not_infer_a_brand() -> None:
    assert service_mentions("Dermal fillers available") == ["Dermal Filler"]


def test_unknown_service_unit_stays_unknown() -> None:
    assert evaluate_service("Botox", ["Botox pricing available"]).unit_type is None
