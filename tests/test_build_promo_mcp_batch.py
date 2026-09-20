"""Regression checks for the transactional promo importer SQL."""
from pathlib import Path


SCRIPT = Path(__file__).resolve().parents[1] / "scripts" / "build_promo_mcp_batch.py"
ATOMIC_WRITES = Path(__file__).resolve().parents[1] / "config" / "sql" / "m019_atomic_change_driven_writes.sql"


def test_importer_does_not_invent_item_unit_price() -> None:
    source = SCRIPT.read_text(encoding="utf-8")

    assert "discount_price','')::real/COALESCE" not in source
    assert "NULLIF(i->>'unit_price','')::numeric" in source


def test_importer_resolves_database_fingerprint_and_preserves_unknown_quantity() -> None:
    source = SCRIPT.read_text(encoding="utf-8")

    assert "computed_fingerprint" in source
    assert "m.offer_fingerprint=r.master->>'offer_fingerprint'" not in source
    assert "COALESCE(NULLIF(i->>'quantity','')::smallint,1)" not in source
    assert "NULLIF(i->>'quantity','')::smallint" in source


def test_atomic_offer_insert_defaults_missing_new_customer_flag_to_false() -> None:
    source = ATOMIC_WRITES.read_text(encoding="utf-8")

    assert "COALESCE((v_master->>'is_new_customer_required')::BOOLEAN, FALSE)" in source
    assert "COALESCE((v_master->>'is_new_customer_required')::BOOLEAN, TRUE)" not in source
