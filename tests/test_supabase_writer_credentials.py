import pytest

from utils.supabase_rest import get_supabase_writer_key


def test_writer_key_is_required_by_default(monkeypatch):
    monkeypatch.delenv("SUPABASE_WRITER_KEY", raising=False)
    monkeypatch.delenv("SUPABASE_SERVICE_ROLE_KEY", raising=False)
    monkeypatch.delenv("ALLOW_SERVICE_ROLE_WRITES", raising=False)
    with pytest.raises(RuntimeError, match="SUPABASE_WRITER_KEY"):
        get_supabase_writer_key()


def test_service_role_requires_explicit_rollback_override(monkeypatch):
    monkeypatch.delenv("SUPABASE_WRITER_KEY", raising=False)
    monkeypatch.setenv("SUPABASE_SERVICE_ROLE_KEY", "service-secret")
    monkeypatch.delenv("ALLOW_SERVICE_ROLE_WRITES", raising=False)
    with pytest.raises(RuntimeError):
        get_supabase_writer_key()
    monkeypatch.setenv("ALLOW_SERVICE_ROLE_WRITES", "true")
    assert get_supabase_writer_key() == "service-secret"
