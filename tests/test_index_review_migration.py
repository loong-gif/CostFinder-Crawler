"""Static contract checks for m012 index review migration."""
from pathlib import Path


MIGRATION = (
    Path(__file__).resolve().parents[1] / "config" / "sql" / "m012_index_review.sql"
)


def test_adds_promotion_fk_and_consumer_indexes() -> None:
    sql = MIGRATION.read_text(encoding="utf-8")
    assert "idx_promo_offer_master_promotion_id" in sql
    assert "idx_promo_offer_master_active_created" in sql
    assert "idx_promo_offer_master_active_featured" in sql
    assert "WHERE is_active = true" in sql
    assert "DROP INDEX IF EXISTS idx_promotions_business_active_dates" in sql
    assert "service_category" not in sql  # column not on live schema yet
