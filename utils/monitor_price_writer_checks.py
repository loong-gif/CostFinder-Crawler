from __future__ import annotations

from psycopg.types.json import Jsonb

from utils.monitor_price_writer_plan import (
    BASE_TABLES, BASE_TABLES_BY_GROUP, BLANKET_REGEX_SQL, LEGACY_TERMS_COLUMNS, TABLES, TERMS,
)
from utils.pipeline_db import literal
from utils.sql_checks import (
    Raw, id_array as _ids, jsonb_literal as _jsonb, raise_if as _raise,
    row_equal_excluding, row_matches, versions_differ,
)


def stale_statements(renderer):
    statements = []
    for group, select, label in (("promotions", renderer.promotions, "promotions"), ("offers", renderer.offers, "offers"),
                                 ("items", renderer.items, "items")):
        pk = {"promotions": "promotion_id", "offers": "id", "items": "offer_item_id"}[group]
        ids = [row[pk] for row in renderer.before[group]]
        statements.append(_raise(
            f"coalesce((select array_agg(x order by x) from ({select}) s(x)), '{{}}'::bigint[]) is distinct from {_ids(ids)}",
            f"stale_before: {label} membership"))
    services = [row["service_id"] for row in renderer.before["services"]]
    statements.append(_raise(
        f"coalesce((select array_agg(service_id order by service_id) from public.clinic_services "
        f"where business_id = {renderer.business}), '{{}}'::bigint[]) is distinct from {_ids(services)}",
        "stale_before: services membership"))
    if "terms" in renderer.before:
        terms = [row["offer_id"] for row in renderer.before["terms"]]
        statements.append(_raise(
            f"coalesce((select array_agg(t.offer_id order by t.offer_id) from public.promo_offer_terms t "
            f"where t.offer_id in ({renderer.offers})), '{{}}'::bigint[]) is distinct from {_ids(terms)}",
            "stale_before: terms membership"))
    tables = {"promotions": "clinic_promotions", "offers": "promo_offer_master", "items": "promo_offer_items",
              "services": "clinic_services", "terms": TERMS}
    for group, rows in renderer.before.items():
        table = tables[group]
        pk = "service_id" if group == "services" else TABLES[table][1]
        for row in rows:
            statements.append(_raise(f"not {row_matches(table, pk, row)}", f"stale_before: {table} {int(row[pk])}"))
    return statements


def protected_sets(renderer):
    by_table = {}
    for table, row_id in renderer.refs:
        by_table.setdefault(table, []).append(row_id)

    def array(table):
        return "'{" + ",".join(str(i) for i in sorted(by_table.get(table, []))) + "}'::bigint[]"

    promotions, offers, items, terms = (array(t) for t in
                                        ("clinic_promotions", "promo_offer_master", "promo_offer_items", TERMS))
    expanded_offers = (f"select id from public.promo_offer_master where id = any({offers}) "
                       f"or promotion_id = any({promotions})")
    expanded_items = (f"select offer_item_id from public.promo_offer_items where offer_item_id = any({items}) "
                      f"or offer_id in ({expanded_offers})")
    return by_table, {"P": promotions, "O": offers, "I": items, "T": terms}, expanded_offers, expanded_items


def protected_snapshot(renderer):
    _, sets, expanded_offers, expanded_items = protected_sets(renderer)
    return (
        "(select coalesce(jsonb_agg(jsonb_build_array(s.k, s.x) order by s.k), '[]'::jsonb) from ("
        f"select 'clinic_promotions:' || p.promotion_id as k, to_jsonb(p) as x from public.clinic_promotions p "
        f"where p.promotion_id = any({sets['P']}) "
        f"union all select 'promo_offer_master:' || m.id, to_jsonb(m) from public.promo_offer_master m "
        f"where m.id in ({expanded_offers}) "
        f"union all select 'promo_offer_items:' || i.offer_item_id, to_jsonb(i) from public.promo_offer_items i "
        f"where i.offer_item_id in ({expanded_items}) "
        f"union all select 'promo_offer_terms:' || t.offer_id, to_jsonb(t) from public.promo_offer_terms t "
        f"where t.offer_id = any({sets['T']}) or t.offer_id in ({expanded_offers})) s)")


def protection_statements(renderer):
    if not renderer.refs:
        return [], []
    by_table, sets, expanded_offers, expanded_items = protected_sets(renderer)
    pk = {"clinic_promotions": "promotion_id", "promo_offer_master": "id", "promo_offer_items": "offer_item_id", TERMS: "offer_id"}
    statements = []
    for table, ids in sorted(by_table.items()):
        listed = "'{" + ",".join(str(i) for i in sorted(ids)) + "}'::bigint[]"
        statements.append(_raise(
            f"(select count(*) from public.{table} where {pk[table]} = any({listed})) <> {len(set(ids))}",
            f"protected_row_missing: {table}"))
    statements.insert(0, "v_protected := " + protected_snapshot(renderer) + ";")
    for op in renderer.operations:
        table = op["table"]
        if op["action"] == "update":
            if table == "clinic_promotions":
                inside = f"{op['id']} = any({sets['P']})"
            elif table == "promo_offer_master":
                inside = f"{op['id']} in ({expanded_offers})"
            elif table == "promo_offer_items":
                inside = f"{op['id']} in ({expanded_items})"
            else:
                inside = f"{op['id']} = any({sets['T']}) or {op['id']} in ({expanded_offers})"
            statements.append(_raise(inside, "protected_operation"))
        parent_promotion, parent_offer = op["values"].get("promotion_id"), op["values"].get("offer_id")
        if isinstance(parent_promotion, int) and parent_promotion > 0:
            statements.append(_raise(f"{parent_promotion} = any({sets['P']})", "protected_operation"))
        if isinstance(parent_offer, int) and parent_offer > 0:
            statements.append(_raise(f"{parent_offer} in ({expanded_offers})", "protected_operation"))
    after = _raise(f"{protected_snapshot(renderer)} is distinct from v_protected", "protected_row_changed")
    return statements, [after]


def requested_statements(renderer):
    statements = []
    for op in renderer.operations:
        table = op["table"]
        pk = TABLES[table][1]
        values = renderer.resolved(op)
        row_id = renderer.row_id(op)
        offer = table == "promo_offer_master"
        conditions = [f"t.{pk} = {row_id}"]
        for key, value in values.items():
            if offer and key in {"discount_amount", "discount_percent"}:
                continue
            conditions.append(f"(to_jsonb(t) -> {literal(key)}) = {_jsonb(value)}")
        if offer:
            regular, discount = "t.regular_price::numeric", "t.discount_price::numeric"
            derived = (f"({regular} is not null and {discount} is not null and {regular} > 0 and {discount} >= 0 "
                       f"and {discount} < {regular})")
            requested = [f"(to_jsonb(t) -> {literal(key)}) = {_jsonb(values[key])}"
                         for key in ("discount_amount", "discount_percent") if key in values]
            fallback = " and ".join(requested) or "true"
            conditions.append(
                f"(case when {derived} then t.discount_amount::numeric is not distinct from round({regular} - {discount}, 2) "
                f"and t.discount_percent::numeric is not distinct from round(({regular} - {discount}) * 100 / {regular}, 2) "
                f"else {fallback} end)")
        label = f"requested_fields_changed: {table} {op['id']}"
        statements.append(_raise(f"not exists (select 1 from public.{table} t where {' and '.join(conditions)})", label))
    return statements


def membership_statements(renderer):
    inserted = {table: [name for (t, _), name in renderer.variables.items() if t == table] for table in BASE_TABLES}
    statements = []
    for group, select, pk, table in (("promotions", renderer.promotions, "promotion_id", "clinic_promotions"),
                                     ("offers", renderer.offers, "id", "promo_offer_master"),
                                     ("items", renderer.items, "offer_item_id", "promo_offer_items")):
        expected = [row[pk] for row in renderer.before[group]] + [Raw(n) for n in inserted[table]]
        statements.append(_raise(
            f"coalesce((select array_agg(x order by x) from ({select}) s(x)), '{{}}'::bigint[]) is distinct from {_ids(expected)}",
            f"source_membership_changed: {group}"))
    for name in inserted["clinic_promotions"]:
        statements.append(_raise(
            f"exists (select 1 from public.clinic_promotions where promotion_id = {name} and business_id is distinct from {renderer.business})",
            "cross_business_source"))
    return statements


def row_state_statements(renderer):
    statements = []
    updates = {(op["table"], op["id"]): op for op in renderer.operations if op["action"] == "update"}
    for table, (group, pk, _) in BASE_TABLES.items():
        for row in renderer.before[group]:
            keys = [k for k in ("business_id", "promotion_id", "offer_id", "membership_plan_id") if k in row]
            if keys:
                conditions = " and ".join(f"(to_jsonb(t) -> {literal(k)}) = {literal(Jsonb(row[k]))}" for k in keys)
                statements.append(_raise(
                    f"not exists (select 1 from public.{table} t where t.{pk} = {int(row[pk])} and {conditions})",
                    f"ownership_changed: {table} {int(row[pk])}"))
            if "is_active" in row:
                op = updates.get((table, row[pk]))
                wanted = op["values"]["is_active"] if op and "is_active" in op["values"] else row["is_active"]
                statements.append(_raise(
                    f"not exists (select 1 from public.{table} t where t.{pk} = {int(row[pk])} "
                    f"and (to_jsonb(t) -> 'is_active') = {literal(Jsonb(wanted))})",
                    f"unexpected_deactivation: {table} {int(row[pk])}"))
    return statements


def offer_statements(renderer):
    business = renderer.business
    blanket = f"coalesce(m.offer_raw_text, '') ~* {literal(BLANKET_REGEX_SQL)}"
    source = f"public.promo_offer_master m where m.promotion_id in ({renderer.promotions})"
    active = f"m.is_active"
    joined = (f"from public.promo_offer_master m join public.promo_offer_items i on i.offer_id = m.id "
              f"where m.promotion_id in ({renderer.promotions}) and m.is_active")

    def exists(condition, category):
        return _raise(f"exists (select 1 {joined} and {condition})", category)

    return [
        _raise(f"exists (select 1 from {source} and (m.business_id is distinct from {business} "
               f"or m.membership_plan_id is not null))", "invalid_offer_source"),
        _raise(f"exists (select 1 from {source} and {active} and not exists "
               f"(select 1 from public.promo_offer_items i where i.offer_id = m.id))", "offer_without_items"),
        exists("i.delivered_units is not null and i.delivered_units <= 0", "invalid_quantity"),
        exists("coalesce(m.is_package, false) and i.unit_price is not null", "package_unit_price"),
        exists("i.service_id is null and btrim(coalesce(i.item_name, '')) = ''", "missing_item_name"),
        exists(f"{blanket} and i.service_id is not null", "blanket_service_mapping"),
        exists(f"i.service_id is not null and not exists (select 1 from public.clinic_services s "
               f"where s.service_id = i.service_id and s.business_id = {business})", "cross_business_mapping"),
        exists(f"not ({blanket}) and not coalesce(m.is_package, false) and m.discount_price is not null "
               f"and (i.unit_price is null or round(i.unit_price * coalesce(i.delivered_units, 1), 2) "
               f"<> round(m.discount_price, 2))", "unit_total_mismatch"),
        _raise(f"exists (select 1 {joined} and not ({blanket}) group by m.id, "
               f"case when i.service_id is not null then 's:' || i.service_id "
               f"else 'n:' || lower(btrim(coalesce(i.item_name, ''))) end having count(*) > 1)", "duplicate_service"),
        _raise(f"exists (select 1 from {source} and {active} and exists (select 1 from public.clinic_promotions p "
               f"where p.promotion_id = m.promotion_id and (not coalesce(p.is_active, false) "
               f"or (p.campaign_end_date is not null and p.campaign_end_date::date < current_date))))",
               "inactive_promotion"),
    ]


def terms_statements(renderer):
    if "terms" not in renderer.before:
        return []
    master = {op["id"] for op in renderer.operations if op["table"] == "promo_offer_master" and op["id"] > 0}
    requested = {op["id"]: set(op["values"]) for op in renderer.operations if op["table"] == TERMS and op["id"] > 0}
    statements = []
    for row in renderer.before["terms"]:
        offer_id = row["offer_id"]
        # 主表 qualification 触发器会同步这些列；Terms 请求自己写的列由 requested_fields 核对
        ignored = {"created_at", "updated_at"} | requested.get(offer_id, set())
        if offer_id in master:
            ignored |= LEGACY_TERMS_COLUMNS
        statements.append(_raise(
            f"not {row_equal_excluding(TERMS, 'offer_id', row, ignored)}",
            f"unexpected_terms_or_trigger_change: {int(offer_id)}"))
    return statements


def affected(renderer):
    """计划行及触发器可能连带改动的行（子行、兄弟行、父行）。"""
    ids = {"promotions": set(), "offers": set(), "items": set()}
    offers = {r["id"]: r for r in renderer.before["offers"]}
    items = {r["offer_item_id"]: r for r in renderer.before["items"]}

    def touch_offer(offer_id):
        ids["offers"].add(offer_id)
        ids["items"].update(i["offer_item_id"] for i in items.values() if i["offer_id"] == offer_id)
        parent = offers.get(offer_id, {}).get("promotion_id")
        if parent is not None:
            ids["promotions"].add(parent)

    for op in renderer.operations:
        table, row_id, values = op["table"], op["id"], op["values"]
        if table == "clinic_promotions":
            ids["promotions"].add(row_id)
            if set(values) - {"is_offer_extracted"}:
                for offer in offers.values():
                    if offer.get("promotion_id") == row_id:
                        touch_offer(offer["id"])
        elif table == "promo_offer_master":
            touch_offer(row_id)
            if isinstance(values.get("promotion_id"), int):
                ids["promotions"].add(values["promotion_id"])
        elif table == "promo_offer_items":
            offer_id = values.get("offer_id", items.get(row_id, {}).get("offer_id"))
            ids["items"].add(row_id)
            if isinstance(offer_id, int):
                touch_offer(offer_id)
        else:
            touch_offer(row_id)
    return ids


def unaffected_statements(renderer):
    """其余行（含兄弟 Offer）的版本不得因本批写入或触发器而变化。"""
    touched = affected(renderer)
    inserted = {group: [Raw(name) for (table, _), name in renderer.variables.items() if table == BASE_TABLES_BY_GROUP[group]]
                for group in touched}
    scopes = {"promotions": f"t.promotion_id in ({renderer.promotions})", "offers": f"t.id in ({renderer.offers})",
              "items": f"t.offer_item_id in ({renderer.items})"}
    statements = []
    for table, (group, pk, _) in BASE_TABLES.items():
        rows = [row for row in renderer.before[group] if row[pk] not in touched[group]]
        skipped = sorted(touched[group]) + inserted[group]
        statements.append(_raise(versions_differ(table, pk, rows, scopes[group], exclude=skipped),
                                 f"unexpected_row_or_trigger_change: {table}"))
    return statements
