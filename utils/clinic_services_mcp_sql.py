"""Render audited clinic-service operations into safe Supabase MCP SQL."""
from __future__ import annotations

import json
from typing import Any, Iterable

from utils.clinic_service_taxonomy import live_clinic_service_payload
from utils.clinic_services_firecrawl import normalize_unit_type


def _sql_json(value: Any) -> str:
    """Return a SQL string literal holding JSON, including untrusted page text."""
    return "'" + json.dumps(value, ensure_ascii=False, separators=(",", ":")).replace("'", "''") + "'"


def render_mcp_operations(operations: Iterable[dict[str, Any]]) -> str:
    """Build idempotent inserts/updates with the approved service-name enum."""
    inserts: list[dict[str, Any]] = []
    updates: list[dict[str, Any]] = []
    for operation in operations:
        raw_payload = operation.get("payload") or {}
        payload = live_clinic_service_payload(raw_payload)
        if "unit_type" in payload:
            payload["unit_type"] = normalize_unit_type(payload["unit_type"])
        if operation.get("action") == "mcp_insert":
            inserts.append(payload)
        elif operation.get("action") == "mcp_update":
            updates.append({"service_id": (operation.get("match") or {}).get("service_id"), **payload})

    statements = ["begin;"]
    if inserts:
        statements.append(
            """with rows as (
  select * from jsonb_to_recordset("""
            + _sql_json(inserts)
            + """::jsonb) as r(
    business_id bigint, service_name text, regular_price numeric,
    unit_type text, service_area text, source_url text
  )
)
insert into public.clinic_services (
  business_id, service_name, regular_price, unit_type, service_area,
  source_url, updated_at
)
select business_id,
  service_name::public.clinic_service_name_enum,
  regular_price, unit_type, service_area, source_url, now()
from rows
where not exists (
  select 1 from public.clinic_services c
  where c.business_id = rows.business_id
    and c.service_name = rows.service_name::public.clinic_service_name_enum
);"""
        )
    if updates:
        statements.append(
            """with rows as (
  select * from jsonb_to_recordset("""
            + _sql_json(updates)
            + """::jsonb) as r(
    service_id bigint, business_id bigint, service_name text,
    regular_price numeric, unit_type text, service_area text, source_url text
  )
)
update public.clinic_services c
set business_id = coalesce(r.business_id, c.business_id),
    service_name = case
      when r.service_name is null then c.service_name
      else r.service_name::public.clinic_service_name_enum
    end,
    regular_price = r.regular_price,
    unit_type = r.unit_type,
    service_area = r.service_area,
    source_url = r.source_url,
    updated_at = now()
from rows r
where c.service_id = r.service_id;"""
        )
    statements.append("commit;")
    return "\n".join(statements)
