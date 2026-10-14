from pathlib import Path


MIGRATION = next(
    (Path(__file__).resolve().parents[1] / "supabase" / "migrations").glob(
        "*_repair_clinic_service_unit_type_unknowns.sql"
    )
)


def test_unknown_unit_repair_removes_fallback_and_restores_nullability() -> None:
    sql = MIGRATION.read_text(encoding="utf-8").lower()

    assert "alter column unit_type drop default" in sql
    assert "alter column unit_type drop not null" in sql
    assert "drop trigger if exists trg_clinic_services_unit_type_fallback" in sql
    assert "drop function if exists public.apply_clinic_service_unit_type_fallback()" in sql


def test_unknown_unit_repair_allows_only_supported_units_or_null() -> None:
    sql = MIGRATION.read_text(encoding="utf-8").lower()

    assert "clinic_services_unit_type_allowed_check" in sql
    assert "unit_type is null or unit_type in" in sql
    assert "'unit', 'syringe', 'vial', 'session', 'treatment', 'area', 'shot'" in sql
    assert "where cs.unit_type in ('treatment', 'others')" in sql
    assert "when e.type_count = 1 then e.explicit_unit" in sql
    assert "else null" in sql
