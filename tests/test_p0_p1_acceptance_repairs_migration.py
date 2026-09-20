from pathlib import Path


SQL = (
    Path(__file__).resolve().parents[1]
    / "supabase"
    / "migrations"
    / "20260911140429_costfinder_p0_p1_acceptance_repairs.sql"
).read_text(encoding="utf-8").lower()


def test_repairs_parent_offer_lifecycle_and_quarantines_unmapped_items() -> None:
    assert "not coalesce(promotion.is_active, false)" in SQL
    assert "item.service_id is null" in SQL
    assert "set is_active = false" in SQL


def test_repairs_deterministic_item_prices_and_regular_prices() -> None:
    assert "offer.price_model = 'per_unit'" in SQL
    assert "unit_price = offer.discount_price" in SQL
    assert "regular_price = discount_price + discount_amount" in SQL
    assert "20 units $189 vs $270 regular." in SQL
    assert "minimum 70 units at $11 per unit - $770 (value $980)" in SQL
    assert "if v_price_model = 'per_unit' then" in SQL
    assert SQL.index("if v_price_model = 'per_unit' then") < SQL.index("if v_has_different_item or v_price_model <> 'total' then")


def test_revokes_public_email_preference_rpc() -> None:
    assert "email_prefs_for_address(text)" in SQL
    assert "from public, anon, authenticated" in SQL
