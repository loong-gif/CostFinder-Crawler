from pathlib import Path


SQL = (
    Path(__file__).parents[1] / "config/sql/m018_claims.sql"
).read_text(encoding="utf-8").lower()


def test_claims_migration_has_core_contract() -> None:
    required = (
        "create type public.claim_status as enum",
        "create table if not exists public.claims",
        "references public.profiles (id) on delete cascade",
        "references public.promo_offer_master (id) on delete cascade",
        "references public.master_business_info (business_id) on delete cascade",
        "claims_active_unique",
        "enable row level security",
        "consumers_read_own_claims",
        "consumers_insert_own_claims",
        "claims_relay_method",
        "trg_claims_updated_at",
    )
    assert all(clause in SQL for clause in required)
