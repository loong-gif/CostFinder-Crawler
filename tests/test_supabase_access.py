"""Tests for schema-neutral Supabase access helpers."""
from __future__ import annotations

from pathlib import Path

import pytest

from utils import supabase_access


class _Client:
    def __init__(self) -> None:
        self.calls: list[dict] = []

    def fetch_rows(self, table, select, *, filters, limit, offset, order):
        self.calls.append(
            {
                "table": table,
                "select": select,
                "filters": filters,
                "limit": limit,
                "offset": offset,
                "order": order,
            }
        )
        return [{"id": offset + index} for index in range(min(limit, max(0, 3 - offset)))]


def test_fetch_all_rows_paginates_and_honors_limit() -> None:
    client = _Client()
    rows = supabase_access.fetch_all_rows(
        client, "clinic_services", "service_id", page_size=2, limit=3, order="service_id.asc"
    )
    assert rows == [{"id": 0}, {"id": 1}, {"id": 2}]
    assert [call["offset"] for call in client.calls] == [0, 2]
    assert [call["limit"] for call in client.calls] == [2, 1]


def test_load_supabase_client_requires_current_connection_settings(monkeypatch, tmp_path: Path) -> None:
    monkeypatch.delenv("SUPABASE_URL", raising=False)
    monkeypatch.delenv("SUPABASE_SERVICE_ROLE_KEY", raising=False)
    monkeypatch.delenv("SUPABASE_SECRET_KEY", raising=False)
    with pytest.raises(RuntimeError, match="Missing SUPABASE_URL"):
        supabase_access.load_supabase_client(tmp_path)
