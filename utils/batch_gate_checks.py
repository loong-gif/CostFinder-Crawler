"""Checks every data-update batch must pass before it counts as complete.

Each query must return zero rows, except rows deliberately retained and
documented in the batch's retained file. All queries run as one read-only
snapshot (``read.sql`` executed by ``utils.pipeline_db.fresh_snapshot``).
"""
from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Callable

from utils import pipeline_db
from utils.pipeline_run import PROJECT_ROOT
from utils.promo_offer_items_db import AGENTS_BLANKET_PATTERN
from utils.supabase_rest import PRODUCTION_PROJECT_REF

OUTPUT_DIR = PROJECT_ROOT / "output" / "pipeline" / "batch_gate"

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
    # C. Blanket offers linked to a specific service (the AGENTS.md pattern plus
    #    "products & services", "all purchases", "all <word> services"). A category-scoped
    #    discount ("all IV services") linked to a service of that category is correct.
    "C_blanket_service_link": r"""
select m.id, m.offer_raw_text, i.item_name, i.offer_item_id
  from promo_offer_master m join promo_offer_items i on i.offer_id = m.id
  join clinic_services s on s.service_id = i.service_id
 where m.is_active
   and m.offer_raw_text ~* '(""" + AGENTS_BLANKET_PATTERN + r"""|products (and|&) services|all purchases|all [a-z-]+ services)'
   and not (m.offer_raw_text !~* '(""" + AGENTS_BLANKET_PATTERN + r"""|products (and|&) services|all purchases)'
            and s.service_name::text ~* ('\m' || substring(lower(m.offer_raw_text) from 'all ([a-z-]+) services') || '\M'))
""",
    # D. Blanket offers stored more than once under one source (the same benefit in
    #    different membership plans or promotions is one offer per source).
    "D_blanket_duplicates": r"""
select business_id, promotion_id, membership_plan_id, offer_raw_text, count(*) as n,
       string_agg(id::text, ',' order by id) as ids
  from promo_offer_master
 where is_active
   and offer_raw_text ~* '(all services|all products|products (and|&) services|all purchases|all [a-z-]+ services)'
 group by business_id, promotion_id, membership_plan_id, offer_raw_text having count(*) > 1
""",
    # E. Stored discount_price with no literal OR arithmetic support in offer text.
    # Computed totals (regular − amount, regular × (1 − percent/100)) pass; literal
    # $X and per-unit rates in text still pass as before.
    "E_price_not_in_text": r"""
with x as (
  select m.id, m.discount_price p, m.offer_raw_text t,
         m.regular_price r, m.discount_percent dpct, m.discount_amount damt,
         m.price_model, coalesce(m.price_model, 'total') pm,
         (select max(i.delivered_units) from promo_offer_items i
           where i.offer_id = m.id) qty,
         (select max(i.unit_price) from promo_offer_items i
           where i.offer_id = m.id) item_unit
    from promo_offer_master m
   where m.is_active and m.discount_price > 0
), amounts as (
  select x.id, x.p, x.t, x.r, x.dpct, x.damt, x.pm, x.qty, x.item_unit,
         a.amount, round((a.amount - trunc(a.amount)) * 100)::int cents
    from x cross join lateral
         (values (x.p), (case when x.qty >= 2 then round(x.p / x.qty, 2) end)) a(amount)
   where a.amount is not null
), pat as (
  select id, p, t, r, dpct, damt, pm, qty, item_unit,
         '(^|[^0-9.,])(' || to_char(trunc(amount), 'FM999999')
         || '|' || to_char(trunc(amount), 'FM999,999') || ')'
         || case when cents = 0 then '(\.00?)?'
                 when cents % 10 = 0 then '\.' || (cents / 10) || '0?'
                 else '\.' || lpad(cents::text, 2, '0') end
         || '([^0-9]|$)' re
    from amounts
), flags as (
  select id, p, t, r, dpct, damt, pm, item_unit,
         bool_or(t ~ re) as literal_in_text,
         (r is not null and dpct is not null
          and abs(p - round(r * (100 - dpct) / 100, 2)) <= 0.02) as pct_arithmetic,
         (r is not null and damt is not null
          and abs(p - (r - damt)) <= 0.02) as amt_arithmetic,
         (pm = 'per_unit' and item_unit is not null
          and abs(item_unit - p) <= 0.02) as item_unit_matches
  from pat
  group by id, p, t, r, dpct, damt, pm, item_unit
)
select id, p as discount_price, t as offer_raw_text
  from flags
 where not (literal_in_text or pct_arithmetic or amt_arithmetic or item_unit_matches)
 order by id
""",
    # F. Collection/card "from $X" stored as a definite total (not price_model=from).
    "F_from_price_dropped": r"""
select m.id, m.price_model, m.offer_raw_text
  from promo_offer_master m
 where m.is_active
   and coalesce(m.price_model, 'total') <> 'from'
   and m.offer_raw_text ~* '\mfrom\s+\$'
""",
    # G. A fixed pack size stated in a total-priced offer that no item delivers.
    #    Thresholds ("minimum 40 units", "50+ units", "up to 4") are stripped first; buy-N-get
    #    offers share one discount over several units and have no single delivered quantity.
    "G_units_in_text_not_delivered": r"""
with q(re) as (
  select '(minimum|min\.?|at least|up to|maximum|max\.?)\s+(of\s+)?\d+'
         '|\d+\s*\+|\d+\s*(units?|sessions?|syringes?|treatments?)\s*(or more|minimum|min\.?|\+)'
         '|\d+[- ]?(units?|sessions?|syringes?|treatments?)[- ]minimum'
), x as (
  select m.id, m.offer_raw_text,
         m.offer_raw_text || ' ; ' || coalesce(array_to_string(m.conditions, ' ; '), '') blob
    from promo_offer_master m
   where m.is_active and coalesce(m.price_model, 'total') = 'total'
     and m.offer_raw_text !~* '\mbuy\s+\d+\M.*\m(get|receive)\M'
)
select x.id, i.offer_item_id, i.delivered_units, x.offer_raw_text
  from x cross join q join promo_offer_items i on i.offer_id = x.id
 where coalesce(i.delivered_units, 1) = 1
   and exists (
     select 1 from regexp_matches(regexp_replace(x.blob, q.re, ' ', 'gi'),
                                  '\m([2-9]|[1-9][0-9]+)\s+(?:units|sessions|syringes|treatments)\M', 'gi') n
      where n[1] not in (select substring(t[1] from '\d+') from regexp_matches(x.blob, '(' || q.re || ')', 'gi') t)
        and not exists (select 1 from promo_offer_items j
                         where j.offer_id = x.id and j.delivered_units = n[1]::int))
""",
    # H. Member sale needs regular_price above the member price.
    #    A labeled compare-at stays regular_price. A two-tier page uses the non-member sale.
    "H_member_nonmember_regular": r"""
select m.id, m.promotion_id, m.discount_price, m.regular_price, m.offer_raw_text
  from promo_offer_master m
  join clinic_promotions p on p.promotion_id = m.promotion_id
 where m.is_active
   and m.is_membership_required
   and (coalesce(p.promotion_content::text, '') || ' ' || m.offer_raw_text) ~* 'non[- ]?member'
   and (m.regular_price is null or m.regular_price <= m.discount_price)
""",
    # I. Offers without items (active always; inactive stale shells after 24h).
    "I_itemless_offer": r"""
select m.id, m.is_active, m.created_at
  from promo_offer_master m
  left join promo_offer_items i on i.offer_id = m.id
 where i.offer_item_id is null
   and (m.is_active or m.created_at < now() - interval '24 hours')
""",
    # J. Non-package offers that still look like unsplit OR lists or multi-item AND composition.
    "J_unsplit_or_and_composition": r"""
select m.id, m.is_package, m.offer_raw_text, count(i.offer_item_id) as item_count
  from promo_offer_master m
  join promo_offer_items i on i.offer_id = m.id
 where m.is_active and coalesce(m.is_package, false) = false
 group by m.id
having count(i.offer_item_id) > 1
    or (
      count(i.offer_item_id) <= 1
      and m.offer_raw_text ~* ',[^$]{0,120}\bor\b'
      and m.offer_raw_text !~* '\bor\s+(more|less|fewer|any)\b'
      and m.offer_raw_text !~* '\b(?:buy|purchase)\b.*\b(?:get|receive)\b'
    )
""",
}


def _row_key(row: dict[str, Any]) -> str:
    for name in ("offer_item_id", "id", "ids", "table_name"):
        if row.get(name) is not None:
            return str(row[name])
    return json.dumps(row, sort_keys=True, default=str)


def snapshot_tables() -> dict[str, tuple[str, str, str]]:
    """每项检查作为快照中的一张"表"：检查查询整体作为子查询。"""
    return {name: (f"({sql.strip()}) gate_rows", "*", "true") for name, sql in CHECKS.items()}


def run_batch_gate(retained: dict[str, dict[str, str]] | None = None, *, run_dir: Path | None = None,
                   read: Callable[[dict[str, tuple[str, str, str]]], dict[str, list[dict[str, Any]]]] | None = None
                   ) -> dict[str, Any]:
    """Run every check; rows are failures unless retained with a documented reason."""
    retained = retained or {}
    tables = snapshot_tables()
    if read is None:
        directory = run_dir or pipeline_db.run_dir(OUTPUT_DIR)
        snapshot = pipeline_db.load_snapshot(pipeline_db.fresh_snapshot(directory, tables), tables, typed=False)
    else:
        snapshot = read(tables)
    results = {}
    for name in CHECKS:
        rows = snapshot[name]
        documented = retained.get(name, {})
        kept = [{**row, "retained_reason": documented[_row_key(row)]} for row in rows
                if str(documented.get(_row_key(row)) or "").strip()]
        failing = [row for row in rows if not str(documented.get(_row_key(row)) or "").strip()]
        results[name] = {"failing": failing, "retained": kept}
    return {"project_ref": PRODUCTION_PROJECT_REF, "checks": results,
            "passed": all(not r["failing"] for r in results.values())}
