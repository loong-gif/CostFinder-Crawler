"""Shared Supabase access helpers for the current CostFinder schema."""
from __future__ import annotations

import os
from pathlib import Path
from typing import Any, Dict, List, Optional

from dotenv import load_dotenv

from utils.supabase_rest import SupabaseRestClient, get_supabase_secret_key, get_supabase_writer_key

_CREDENTIALS = {
    "service": lambda: os.getenv("SUPABASE_SERVICE_ROLE_KEY") or os.getenv("SUPABASE_SECRET_KEY"),
    "secret": get_supabase_secret_key,
    "writer": get_supabase_writer_key,
    "reader": lambda: os.getenv("SUPABASE_PUBLISHABLE_KEY") or os.getenv("SUPABASE_ANON_KEY"),
}


def load_supabase_client(project_root: Optional[Path] = None, *, role: str = "service") -> SupabaseRestClient:
    """Create a REST client; ``role`` picks the credential (service, secret, writer or read-only reader)."""
    root = project_root or Path(__file__).resolve().parents[1]
    load_dotenv(root / ".env")
    base_url = (os.getenv("SUPABASE_URL") or "").strip()
    key = _CREDENTIALS[role]()
    if not base_url or not key:
        raise RuntimeError(f"Missing SUPABASE_URL or the Supabase {role} credential")
    return SupabaseRestClient(base_url, key)


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
