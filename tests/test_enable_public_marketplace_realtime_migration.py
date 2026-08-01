"""Static safety checks for the public marketplace Realtime migration."""

from pathlib import Path
import re


ROOT = Path(__file__).resolve().parents[1]
MIGRATION = next(
    (ROOT / "supabase" / "migrations").glob(
        "*_enable_public_marketplace_realtime.sql"
    )
)

PUBLIC_MARKETPLACE_TABLES = {
    "promo_offer_master",
    "promo_offer_items",
    "clinic_services",
    "clinic_promotions",
    "master_business_info",
    "clinic_memberships",
}


def migration_sql() -> str:
    return MIGRATION.read_text(encoding="utf-8").lower()


def test_migration_publishes_only_public_marketplace_tables() -> None:
    sql = migration_sql()
    table_array = re.search(
        r"marketplace_tables constant text\[\]\s*:=\s*array\[(.*?)\];",
        sql,
        re.DOTALL,
    )
    assert table_array is not None
    published_tables = set(re.findall(r"'([a-z_]+)'", table_array.group(1)))

    assert published_tables == PUBLIC_MARKETPLACE_TABLES
    assert "notifications" not in sql
    assert "messages" not in sql
    assert "add tables in schema" not in sql


def test_migration_is_idempotent_and_preserves_existing_rls_contracts() -> None:
    sql = migration_sql()

    assert "pg_publication_tables" in sql
    assert "if not exists" in sql
    assert "alter publication supabase_realtime add table" in sql
    assert "replica identity" not in sql
    assert "create policy" not in sql
    assert "disable row level security" not in sql
