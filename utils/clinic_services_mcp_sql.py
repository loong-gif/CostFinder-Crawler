"""Render audited clinic-service operations into safe Supabase MCP SQL."""
from __future__ import annotations

import json
from typing import Any, Iterable

from utils.offer_field_normalize import normalize_service_area


def _sql_json(value: Any) -> str:
    """Return a SQL string literal holding JSON, including untrusted page text."""
    return "'" + json.dumps(value, ensure_ascii=False, separators=(",", ":")).replace("'", "''") + "'"


def render_mcp_operations(operations: Iterable[dict[str, Any]]) -> str:
    """Build idempotent inserts/updates with enum casts required by clinic_services."""
    inserts: list[dict[str, Any]] = []
    updates: list[dict[str, Any]] = []
    for operation in operations:
        raw_payload = operation.get("payload") or {}
        payload = {
            **raw_payload,
            "service_area": normalize_service_area(raw_payload.get("service_area")),
        }
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
    business_id bigint, service_name text, service_name_raw text, regular_price numeric,
    unit_type text, service_category text, service_area text, source_url text
  )
)
insert into public.clinic_services (
  business_id, service_name, service_name_raw, regular_price, unit_type,
  service_category, service_area, source_url, updated_at
)
select business_id, service_name, service_name_raw, regular_price,
  unit_type::public.unit_type_enum, service_category::public.service_category_enum,
  service_area::public.service_area_enum, source_url, now()
from rows
where not exists (
  select 1 from public.clinic_services c
  where c.business_id = rows.business_id
    and upper(c.service_name) = upper(rows.service_name)
);"""
        )
    if updates:
        statements.append(
            """with rows as (
  select * from jsonb_to_recordset("""
            + _sql_json(updates)
            + """::jsonb) as r(
    service_id bigint, business_id bigint, service_name text, service_name_raw text,
    regular_price numeric, unit_type text, service_category text, service_area text, source_url text
  )
)
update public.clinic_services c
set business_id = r.business_id,
    service_name = r.service_name,
    service_name_raw = r.service_name_raw,
    regular_price = r.regular_price,
    unit_type = r.unit_type::public.unit_type_enum,
    service_category = r.service_category::public.service_category_enum,
    service_area = r.service_area,
    source_url = r.source_url,
    updated_at = now()
from rows r
where c.service_id = r.service_id;"""
        )
    statements.append("commit;")
    return "\n".join(statements)
