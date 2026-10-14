"""Checks every data-update batch must pass before it counts as complete.

Each query must return zero rows, except rows deliberately retained and
documented in the batch's retained file. Queries run on the linked production
project through ``supabase db query --linked`` and are read-only.
"""
from __future__ import annotations

import json
from pathlib import Path
import subprocess
from typing import Any, Callable

from utils.supabase_rest import PRODUCTION_PROJECT_REF

CHECKS: dict[str, str] = {
    # A. Tables created in public (backups, staging, scratch).
    "A_scratch_tables": r"""
select table_name from information_schema.tables
 where table_schema = 'public'
   and (table_name like 'qa\_%' or table_name like 'bak\_%' or table_name like '%backup%')
""",
    # B. Visible estimates.
    "B_visible_estimates": r"""
select id, offer_raw_text from promo_offer_master
 where is_active and offer_raw_text ~* '\(est\.?\)'
""",
    # C. Blanket offers linked to a specific service (superset of the AGENTS.md check;
    #    also "products & services", "all purchases", "all <word> services").
    "C_blanket_service_link": r"""
select m.id, m.offer_raw_text, i.item_name, i.offer_item_id
  from promo_offer_master m join promo_offer_items i on i.offer_id = m.id
 where m.is_active and i.service_id is not null
   and m.offer_raw_text ~* ('(all services|any service|all products|all treatments|'
                            || 'products (and|&) services|gift card|skin ?care products|'
                            || 'all purchases|all [a-z-]+ services)')
""",
    # D. Blanket offers stored more than once at one clinic.
    "D_blanket_duplicates": r"""
select business_id, offer_raw_text, count(*) as n, string_agg(id::text, ',') as ids
  from promo_offer_master
 where is_active
   and offer_raw_text ~* '(all services|all products|products (and|&) services|all purchases|all [a-z-]+ services)'
 group by business_id, offer_raw_text having count(*) > 1
""",
    # E. A price that does not appear in the offer text ("$1,499", "$1499.00",
    #    "$10.5", or the per-unit rate of a quantity offer).
    "E_price_not_in_text": r"""
with x as (
  select m.id, m.discount_price p, m.offer_raw_text t,
         (select max(i.delivered_units) from promo_offer_items i
           where i.offer_id = m.id) qty
    from promo_offer_master m
   where m.is_active and m.discount_price > 0
), amounts as (
  select x.id, x.p, x.t, a.amount, round((a.amount - trunc(a.amount)) * 100)::int cents
    from x cross join lateral
         (values (x.p), (case when x.qty >= 2 then round(x.p / x.qty, 2) end)) a(amount)
   where a.amount is not null
), pat as (
  select id, p, t,
         '(^|[^0-9.,])(' || to_char(trunc(amount), 'FM999999')
         || '|' || to_char(trunc(amount), 'FM999,999') || ')'
         || case when cents = 0 then '(\.00?)?'
                 when cents % 10 = 0 then '\.' || (cents / 10) || '0?'
                 else '\.' || lpad(cents::text, 2, '0') end
         || '([^0-9]|$)' re
    from amounts
)
select id, p as discount_price, t as offer_raw_text
  from pat group by id, p, t having bool_and(t !~ re) order by id
""",
}


def _row_key(row: dict[str, Any]) -> str:
    for name in ("offer_item_id", "id", "ids", "table_name"):
        if row.get(name) is not None:
            return str(row[name])
    return json.dumps(row, sort_keys=True, default=str)


def linked_query(sql: str, *, root: Path) -> list[dict[str, Any]]:
    linked = json.loads((root / "supabase/.temp/linked-project.json").read_text())
    if linked.get("ref") != PRODUCTION_PROJECT_REF:
        raise RuntimeError("database_project_mismatch")
    # The Management API login occasionally drops the connection; retry the call, never the verdict.
    for _attempt in range(3):
        result = subprocess.run(["supabase", "db", "query", "--linked", "-o", "json", sql],
                                cwd=root, capture_output=True, text=True, timeout=300)
        if result.returncode == 0:
            break
    if result.returncode != 0:
        raise RuntimeError("linked_query_failed:" + result.stderr.strip()[-300:])
    start = result.stdout.index("{")
    payload, _ = json.JSONDecoder().raw_decode(result.stdout[start:])
    return payload["rows"]


def run_batch_gate(root: Path, retained: dict[str, dict[str, str]] | None = None,
                   query: Callable[[str], list[dict[str, Any]]] | None = None) -> dict[str, Any]:
    """Run every check; rows are failures unless retained with a documented reason."""
    retained = retained or {}
    query = query or (lambda sql: linked_query(sql, root=root))
    results = {}
    for name, sql in CHECKS.items():
        rows = query(sql)
        documented = retained.get(name, {})
        kept = [{**row, "retained_reason": documented[_row_key(row)]} for row in rows
                if str(documented.get(_row_key(row)) or "").strip()]
        failing = [row for row in rows if not str(documented.get(_row_key(row)) or "").strip()]
        results[name] = {"failing": failing, "retained": kept}
    return {"project_ref": PRODUCTION_PROJECT_REF, "checks": results,
            "passed": all(not r["failing"] for r in results.values())}
