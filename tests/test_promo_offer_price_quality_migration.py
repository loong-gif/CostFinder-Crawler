from pathlib import Path


SQL = (Path(__file__).parents[1] / "config/sql/m023_promo_offer_price_quality.sql").read_text()


def test_migration_handles_each_price_model_and_master_resync() -> None:
    assert "v_price_model = 'per_unit'" in SQL
    assert "v_price_model <> 'total'" in SQL
    assert "UPDATE public.promo_offer_items" in SQL
    assert "SET discount_amount = round(regular_price - discount_price, 2)" in SQL
    assert "AFTER UPDATE OF discount_price, price_model" in SQL


def test_migration_protects_review_queue_with_rls_and_no_public_access() -> None:
    assert "ALTER TABLE public.promo_offer_quality_reviews ENABLE ROW LEVEL SECURITY" in SQL
    assert "REVOKE ALL ON TABLE public.promo_offer_quality_reviews FROM anon, authenticated" in SQL
    assert "chk_promo_offer_derived_discount_amount" in SQL
    assert "chk_promo_offer_derived_discount_percent" in SQL
    assert "SET search_path = public" in SQL


def test_follow_up_migration_pins_live_trigger_function_search_paths() -> None:
    sql = (Path(__file__).parents[1] / "config/sql/m024_promo_offer_price_function_search_path.sql").read_text()
    assert sql.count("SET search_path = public") == 3
