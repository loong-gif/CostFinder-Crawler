from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
NORMALIZE_SQL = (
    ROOT / "supabase/migrations/20260915062927_normalize_offer_prices.sql"
).read_text(encoding="utf-8").lower()
INVARIANT_SQL = (
    ROOT / "supabase/migrations/20260915063034_enforce_offer_price_invariants.sql"
).read_text(encoding="utf-8").lower()


def test_price_migration_is_split_into_cleanup_and_enforcement() -> None:
    assert "update public.promo_offer_master" in NORMALIZE_SQL
    assert "create or replace function public.sync_non_package_offer_prices" in NORMALIZE_SQL
    assert "create constraint trigger" in INVARIANT_SQL
    assert "initially deferred" in INVARIANT_SQL


def test_discount_uses_total_master_price_for_every_non_package_item() -> None:
    assert "v_price_model" not in NORMALIZE_SQL
    assert "round(v_discount_price / v_quantity, 4)" in NORMALIZE_SQL
    assert "price_model" in NORMALIZE_SQL
    assert "if v_price_model" not in NORMALIZE_SQL


def test_regular_price_flows_from_service_and_quantity_changes() -> None:
    assert "round(new.regular_price * coalesce(item.quantity, 1), 2)" in NORMALIZE_SQL
    assert "after update of regular_price" in NORMALIZE_SQL
    assert "update of offer_id, service_id, quantity" in NORMALIZE_SQL


def test_invariant_uses_cent_rounding_and_skips_packages() -> None:
    assert "master.is_package = false" in INVARIANT_SQL
    assert "round(master.discount_price, 2)" in INVARIANT_SQL
    assert "round(item.unit_price * coalesce(item.quantity, 1), 2)" in INVARIANT_SQL
    assert "round(master.regular_price, 2)" in INVARIANT_SQL
    assert "round(service.regular_price * coalesce(item.quantity, 1), 2)" in INVARIANT_SQL
