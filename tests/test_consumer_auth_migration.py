from pathlib import Path


SQL = (
    Path(__file__).parents[1]
    / "config/sql/m017_consumer_auth_profiles.sql"
).read_text(encoding="utf-8").lower()


def test_consumer_profiles_migration_enforces_owner_access() -> None:
    required = (
        "references auth.users(id) on delete cascade",
        "enable row level security",
        "to authenticated",
        "(select auth.uid()) = id",
        "with check",
        "security definer",
        "set search_path = ''",
        "raw_user_meta_data ->> 'role'",
        "revoke all on function public.handle_new_consumer() from public",
        "after insert on auth.users",
    )
    assert all(clause in SQL for clause in required)
