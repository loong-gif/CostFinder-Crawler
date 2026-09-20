from pathlib import Path


SQL = (Path(__file__).parents[1] / "supabase/migrations/20260912090000_repair_offer_quality.sql").read_text()
GUARD_SQL = (Path(__file__).parents[1] / "supabase/migrations/20260912090100_offer_quality_activation_guards.sql").read_text()
FINGERPRINT_SQL = (Path(__file__).parents[1] / "supabase/migrations/20260912089900_fix_membership_offer_fingerprint_scope.sql").read_text()


def test_offer_quality_repair_is_source_and_timestamp_guarded() -> None:
    assert "old_updated_at timestamptz NOT NULL" in SQL
    assert "m.updated_at=p.old_updated_at" in SQL
    assert "offer quality repair precondition failed" in SQL


def test_offer_quality_repair_quarantines_ambiguous_prices_and_enforces_activation() -> None:
    assert "ambiguous per-unit price basis" in SQL
    assert "CREATE OR REPLACE FUNCTION public.prevent_incomplete_offer_activation()" in GUARD_SQL
    assert "item.unit_price IS NULL OR abs(item.unit_price-NEW.discount_price)>0.011" in GUARD_SQL
    assert "CREATE TRIGGER trg_prevent_incomplete_offer_activation" in GUARD_SQL


def test_offer_quality_repair_canonicalizes_item_names_and_remaps_clear_sources() -> None:
    assert "offer_quality_item_remap" in SQL
    assert "'Dermal Filler', 'Lip Filler'" in SQL
    assert "i.item_name IS DISTINCT FROM service.service_name" in SQL


def test_membership_fingerprint_scope_matches_the_application_contract() -> None:
    assert "'m:' || p_membership_plan_id::text" in FINGERPRINT_SQL
