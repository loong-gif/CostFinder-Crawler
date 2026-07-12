"""Minimal Supabase REST client for CostFinder promos.

Shared single implementation — all active code paths import from here.
Archived scripts keep their own copies.
"""
from __future__ import annotations

from typing import Any, Dict, List, Optional

import requests


class SupabaseRestClient:
    def __init__(self, base_url: str, service_role_key: str):
        self.base_url = base_url.rstrip("/") + "/rest/v1"
        self.session = requests.Session()
        self.session.trust_env = False  # avoid proxy hijacking in automation
        self.session.headers.update(
            {
                "apikey": service_role_key,
                "Authorization": f"Bearer {service_role_key}",
                "Accept": "application/json",
                "Content-Type": "application/json",
            }
        )

    def fetch_rows(
        self,
        table: str,
        select: str,
        *,
        filters: Optional[Dict[str, str]] = None,
        limit: Optional[int] = None,
        offset: Optional[int] = None,
        order: Optional[str] = None,
    ) -> List[Dict[str, Any]]:
        params: Dict[str, str] = {"select": select}
        if filters:
            params.update(filters)
        if limit is not None:
            params["limit"] = str(limit)
        if offset is not None:
            params["offset"] = str(offset)
        if order:
            params["order"] = order
        response = self.session.get(f"{self.base_url}/{table}", params=params, timeout=90)
        response.raise_for_status()
        return response.json()

    def update_row(
        self, table: str, row_id_or_filters: Any, payload: Dict[str, Any]
    ) -> List[Dict[str, Any]]:
        """Update rows matching a filter or a simple id.

        - If row_id_or_filters is a dict, it is used directly as URL params (e.g.
          ``{"promo_website_id": "eq.123"}``).
        - Otherwise it is treated as a simple scalar id and mapped to ``id=eq.<value>``.
        """
        if isinstance(row_id_or_filters, dict):
            params = dict(row_id_or_filters)
        else:
            params = {"id": f"eq.{row_id_or_filters}"}
        response = self.session.patch(
            f"{self.base_url}/{table}",
            params=params,
            json=payload,
            headers={"Prefer": "return=representation"},
            timeout=90,
        )
        response.raise_for_status()
        return response.json()

    def insert_rows(
        self, table: str, rows: List[Dict[str, Any]]
    ) -> List[Dict[str, Any]]:
        response = self.session.post(
            f"{self.base_url}/{table}",
            json=rows,
            headers={"Prefer": "return=representation"},
            timeout=90,
        )
        response.raise_for_status()
        return response.json()

    def delete_rows(
        self,
        table: str,
        filters: Dict[str, str],
    ) -> List[Dict[str, Any]]:
        """Delete rows matching PostgREST filters."""
        response = self.session.delete(
            f"{self.base_url}/{table}",
            params=dict(filters),
            headers={"Prefer": "return=representation"},
            timeout=90,
        )
        response.raise_for_status()
        if not response.text.strip():
            return []
        return response.json()

    def upsert_rows(
        self,
        table: str,
        rows: List[Dict[str, Any]],
        *,
        on_conflict: Optional[str] = None,
    ) -> List[Dict[str, Any]]:
        """Upsert rows via POST with resolution=merge-duplicates.

        Parameters
        ----------
        on_conflict : str, optional
            PostgREST ``on_conflict`` column name for merge-duplicates.
        """
        headers = {"Prefer": "return=representation,resolution=merge-duplicates"}
        if on_conflict:
            headers["Prefer"] += f",on_conflict={on_conflict}"
        response = self.session.post(
            f"{self.base_url}/{table}",
            json=rows,
            headers=headers,
            timeout=90,
        )
        response.raise_for_status()
        return response.json()

    def rpc(self, function: str, payload: Optional[Dict[str, Any]] = None) -> Any:
        response = self.session.post(
            f"{self.base_url}/rpc/{function}",
            json=payload or {},
            timeout=90,
        )
        response.raise_for_status()
        if not response.text.strip():
            return None
        return response.json()


def get_supabase_writer_key() -> str:
    """Return the restricted writer credential for active application paths.

    Service-role credentials are reserved for migrations/administration. A
    temporary explicit override exists only for controlled rollback diagnostics.
    """
    import os

    writer_key = os.getenv("SUPABASE_WRITER_KEY")
    if writer_key:
        return writer_key
    if os.getenv("ALLOW_SERVICE_ROLE_WRITES", "false").strip().lower() in {"1", "true", "yes", "on"}:
        service_key = os.getenv("SUPABASE_SERVICE_ROLE_KEY")
        if service_key:
            return service_key
    raise RuntimeError(
        "Missing SUPABASE_WRITER_KEY; service-role credentials are reserved for migrations "
        "(set ALLOW_SERVICE_ROLE_WRITES=true only for controlled rollback diagnostics)"
    )
