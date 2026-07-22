"""m011 migration is deprecated — live schema already deployed."""
from pathlib import Path


MIGRATION = (
    Path(__file__).resolve().parents[1]
    / "config"
    / "sql"
    / "m011_new_schema_alignment.sql"
)


def test_migration_marked_deprecated() -> None:
    sql = MIGRATION.read_text(encoding="utf-8")
    assert "DEPRECATED" in sql
    assert "BEGIN;" not in sql
