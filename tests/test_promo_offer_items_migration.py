"""Static contract checks for the promo_offer_items migration."""
from pathlib import Path


MIGRATION = (
    Path(__file__).resolve().parents[1]
    / "config"
    / "sql"
    / "m009_promo_offer_items.sql"
)


def migration_sql() -> str:
    return MIGRATION.read_text(encoding="utf-8")


def test_creates_normalized_offer_items_table() -> None:
    sql = migration_sql()
    assert "CREATE TABLE IF NOT EXISTS promo_offer_items" in sql
    assert "offer_id BIGINT NOT NULL REFERENCES promo_offer_master(id) ON DELETE CASCADE" in sql
    assert "service_id BIGINT REFERENCES clinic_services(service_id) ON DELETE SET NULL" in sql
    assert "CHECK (quantity IS NULL OR quantity > 0)" in sql
    assert "sort_order" not in sql


def test_adds_offer_type_and_price_model_on_master() -> None:
    sql = migration_sql()
    assert "ADD COLUMN IF NOT EXISTS offer_type" in sql
    assert "ADD COLUMN IF NOT EXISTS price_model" in sql
    assert "'single'" in sql and "'package'" in sql
    assert "'total'" in sql and "'per_unit'" in sql and "'from'" in sql


def test_moves_service_id_off_master_onto_items() -> None:
    sql = migration_sql()
    assert "LEFT JOIN clinic_services" in sql
    assert "cs.service_name" in sql
    assert "DROP CONSTRAINT IF EXISTS fk_offer_service" in sql
    assert "DROP COLUMN IF EXISTS service_id" in sql
    assert "quantity," in sql
    assert "NULL," in sql  # quantity stays NULL unless stated
    assert "package_content" not in sql
    assert "is_package" not in sql
    assert "delivered_unit" not in sql


def test_migration_is_transactional_and_validates_coverage() -> None:
    sql = migration_sql()
    assert sql.lstrip().startswith("BEGIN;")
    assert sql.rstrip().endswith("COMMIT;")
    assert "ALTER TABLE promo_offer_items ENABLE ROW LEVEL SECURITY" in sql
    assert "RAISE EXCEPTION" in sql
    assert "offer rows without promo_offer_items" in sql
