"""Shared Supabase access helpers for the current CostFinder schema."""
from __future__ import annotations

import os
from pathlib import Path
from typing import Any, Dict, List, Optional

from dotenv import load_dotenv

from utils.supabase_rest import SupabaseRestClient


def load_supabase_client(project_root: Optional[Path] = None) -> SupabaseRestClient:
    """Create a REST client using the configured server-side credential."""
    root = project_root or Path(__file__).resolve().parents[1]
    load_dotenv(root / ".env")
    base_url = os.getenv("SUPABASE_URL")
    service_role_key = os.getenv("SUPABASE_SERVICE_ROLE_KEY") or os.getenv("SUPABASE_SECRET_KEY")
    if not base_url or not service_role_key:
        raise RuntimeError("Missing SUPABASE_URL and a Supabase server-side credential")
    return SupabaseRestClient(base_url, service_role_key)


def fetch_all_rows(
    client: SupabaseRestClient,
    table: str,
    select: str,
    *,
    filters: Optional[Dict[str, str]] = None,
    page_size: int = 500,
    order: Optional[str] = None,
    limit: Optional[int] = None,
) -> List[Dict[str, Any]]:
    """Read a complete, bounded result set through PostgREST pagination."""
    rows: List[Dict[str, Any]] = []
    offset = 0
    while limit is None or len(rows) < limit:
        batch_size = min(page_size, limit - len(rows)) if limit is not None else page_size
        batch = client.fetch_rows(
            table, select, filters=filters, limit=batch_size, offset=offset, order=order
        )
        if not batch:
            break
        rows.extend(batch)
        if len(batch) < batch_size:
            break
        offset += batch_size
    return rows
