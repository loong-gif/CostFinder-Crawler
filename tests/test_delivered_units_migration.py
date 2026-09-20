from pathlib import Path


MIGRATION = (
    Path(__file__).resolve().parents[1]
    / "supabase"
    / "migrations"
    / "20260915071205_rename_promo_offer_items_quantity_to_delivered_units.sql"
)


def migration_sql() -> str:
    return MIGRATION.read_text(encoding="utf-8")


def test_renames_the_live_column_without_adding_a_compatibility_column() -> None:
    sql = migration_sql()
    assert "RENAME COLUMN quantity TO delivered_units" in sql
    assert "ADD COLUMN" not in sql
    assert "DROP COLUMN delivered_units" not in sql


def test_redefines_quantity_dependent_database_functions() -> None:
    sql = migration_sql()
    for function_name in (
        "deactivate_offer_with_unresolved_item",
        "fn_offer_items_signature",
        "normalize_promo_offer_item_unit_price",
        "prevent_incomplete_offer_activation",
        "sync_non_package_offer_prices",
        "sync_offer_regular_price_from_service",
        "validate_non_package_offer_prices",
    ):
        assert f"FUNCTION public.{function_name}" in sql
    assert ".quantity" not in sql


def test_validates_the_post_rename_shape() -> None:
    sql = migration_sql()
    assert "column_name = 'delivered_units'" in sql
    assert "column_name = 'quantity'" in sql
    assert "RAISE EXCEPTION" in sql
