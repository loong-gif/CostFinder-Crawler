"""Migration preflight checks for M016 guardrails."""
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def test_m016_sql_exists_and_idempotent() -> None:
    sql = (ROOT / "config" / "sql" / "m016_extraction_quality_guardrails.sql").read_text(
        encoding="utf-8"
    )
    assert "chk_promo_has_discount" in sql
    assert "IF NOT EXISTS" in sql
    assert "idx_promo_offer_active_fingerprint" in sql
