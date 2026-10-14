from pathlib import Path


SQL = (
    Path(__file__).resolve().parents[1]
    / "supabase"
    / "migrations"
    # Kept without a timestamp so `supabase db push` never applies it.
    / "consolidate_blanket_offer_rows.sql"
).read_text(encoding="utf-8")


def test_blanket_offer_migration_consolidates_only_the_confirmed_groups() -> None:
    for group in (
        "ARRAY[10541, 10542, 10543]",
        "ARRAY[10559, 10560, 10561]",
        "ARRAY[10562, 10563, 10564, 10565]",
        "ARRAY[10570, 10571, 10572]",
        "ARRAY[10619, 10620, 10621, 10622, 10623, 10624, 10625, 10626, 10627, 10628]",
        "ARRAY[10629, 10630, 10631]",
        "ARRAY[10640, 10641, 10642, 10643, 10644, 10645, 10646, 10647, 10648]",
    ):
        assert group in SQL
    assert "service_id = NULL" in SQL
    assert "SET is_active = false" in SQL


def test_blanket_offer_migration_prevents_future_service_links_without_new_tables() -> None:
    assert "CREATE OR REPLACE FUNCTION public.is_blanket_offer_text" in SQL
    assert "BEFORE INSERT OR UPDATE OF offer_id, service_id" in SQL
    assert "NEW.service_id := NULL" in SQL
    assert "ALTER TABLE" not in SQL


def test_blanket_offer_migration_keeps_blankets_active_without_price_or_mapping_gate_failures() -> None:
    assert "NOT public.is_blanket_offer_text(NEW.offer_raw_text)" in SQL
    assert "NOT public.is_blanket_offer_text(offer.offer_raw_text)" in SQL
    assert "NOT public.is_blanket_offer_text(master.offer_raw_text)" in SQL
    assert "CREATE OR REPLACE FUNCTION public.sync_non_package_offer_prices" in SQL
    assert "public.is_blanket_offer_text(v_offer_raw_text)" in SQL
