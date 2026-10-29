"""把已就绪的 monitor 计划渲染为单个 DO 块（pending SQL）；Python 不连库、不写库。

DO 块先在 SQL 中核对业务图快照、保护项与字段白名单，再写入，写后核对不变量，任一不符 raise 并整体回滚。
"""
from __future__ import annotations

from datetime import datetime
from urllib.parse import urlsplit

from psycopg.types.json import Jsonb

from utils.pipeline_db import do_block, literal, plan_digest
from utils.promo_offer_items_db import AGENTS_BLANKET_PATTERN, _BLANKET_OFFER_RE
from utils.schema_contract import (
    CLINIC_PROMOTION_COLUMNS, OFFER_ITEM_COLUMNS, OFFER_MASTER_COLUMNS,
)
from utils.sql_checks import (
    Raw, id_array as _ids, jsonb_literal as _jsonb, raise_if as _raise,
    row_equal_excluding, row_matches, sql_value as _sql, versions_differ, wire as _wire, write_value as _write_value,
)

TERMS = "promo_offer_terms"
SERVICE_COLUMNS = "service_id,business_id,service_name,service_area,source_url"
BASE_TABLES = {
    "clinic_promotions": ("promotions", "promotion_id", set(CLINIC_PROMOTION_COLUMNS) | {"is_offer_extracted"}),
    "promo_offer_master": ("offers", "id", set(OFFER_MASTER_COLUMNS) | {"is_package"}),
    "promo_offer_items": ("items", "offer_item_id", set(OFFER_ITEM_COLUMNS)),
}
TABLES = {**BASE_TABLES, TERMS: ("terms", "offer_id", {
    "offer_id", "membership_plan_id", "min_quantity", "min_unit", "promo_code", "raw_terms",
    "is_new_customer_required", "is_returning_customer_required", "is_membership_required",
    "redemption_frequency", "requires_consultation", "is_not_combinable", "is_one_per_patient",
})}
BASE_TABLES_BY_GROUP = {group: table for table, (group, _, _) in BASE_TABLES.items()}
BEFORE_GROUPS = {"promotions", "offers", "items", "services"}
# 主表（promo_offer_master）的 qualification 触发器同步到 Terms 的列
LEGACY_TERMS_COLUMNS = {"raw_terms", "membership_plan_id", "is_new_customer_required",
                        "is_returning_customer_required", "is_membership_required"}
BLANKET_CHECK_SQL = f"""
select m.id, m.offer_raw_text, i.item_name
from public.promo_offer_master m
join public.promo_offer_items i on i.offer_id = m.id
where m.is_active
  and i.service_id is not null
  and m.offer_raw_text ~* '({AGENTS_BLANKET_PATTERN})'
"""
# 与 is_blanket_offer_text 同一组词；Python 的 \b 在 PostgreSQL 正则中写作 \y。
BLANKET_REGEX_SQL = _BLANKET_OFFER_RE.pattern.replace(r"\b", r"\y") + "|" + AGENTS_BLANKET_PATTERN
# normalize_source_url 在 SQL 中的等价正则：协议、主机、端口、路径、查询
URL_KEY_PATTERN = r"^(?:[hH][tT][tT][pP][sS]?)://([^/?#@:]+)(?::([0-9]+))?((?:/[^?#]*)?)(?:[?]([^#]*))?(?:#.*)?$"
_VARIABLE_PREFIX = {"clinic_promotions": "v_promotion", "promo_offer_master": "v_offer", "promo_offer_items": "v_item"}


class WriterError(ValueError):
    """安全的类别名，不含连接串或数据库错误文本。"""


def normalize_source_url(value):
    try:
        parsed = urlsplit(str(value or "").strip())
        if parsed.scheme.lower() not in {"http", "https"} or not parsed.hostname:
            raise ValueError
        if parsed.username or parsed.password:
            raise ValueError
        host = parsed.hostname.lower().removeprefix("www.")
        port = parsed.port
        suffix = f":{port}" if port and port not in {80, 443} else ""
        return host + suffix + parsed.path.rstrip("/") + ("?" + parsed.query if parsed.query else "")
    except ValueError:
        raise WriterError("invalid_source") from None


def _canonical_row(row, group):
    result = _wire(dict(row))
    if group == "items":
        for alias, physical in (("id", "offer_item_id"), ("quantity", "delivered_units")):
            if alias in result:
                if physical in result and result[physical] != result[alias]:
                    raise WriterError("conflicting_alias")
                result[physical] = result.pop(alias)
    for key in ("created_at", "updated_at"):
        value = result.get(key)
        if isinstance(value, str):
            try:
                result[key] = datetime.fromisoformat(value.replace("Z", "+00:00")).isoformat()
            except ValueError:
                pass
    return result


def _graph(graph):
    groups = [("promotions", "promotion_id"), ("offers", "id"), ("items", "offer_item_id"), ("services", "service_id")]
    if "terms" in graph:
        groups.append(("terms", "offer_id"))
    return {group: sorted((_canonical_row(row, group) for row in graph.get(group, [])), key=lambda row: row[pk])
            for group, pk in groups}


def source_graph(graph, source):
    url = normalize_source_url(source["url"])
    promotions = []
    for row in graph["promotions"]:
        try:
            matches = normalize_source_url(row.get("source_url")) == url
        except WriterError:
            matches = False
        if matches:
            promotions.append(row)
    ids = {row["promotion_id"] for row in promotions}
    offers = [row for row in graph["offers"] if row.get("promotion_id") in ids]
    offer_ids = {row["id"] for row in offers}
    result = dict(promotions=promotions, offers=offers,
                  items=[row for row in graph["items"] if row["offer_id"] in offer_ids],
                  services=graph["services"])
    if "terms" in graph:
        result["terms"] = [row for row in graph["terms"] if row.get("offer_id") in offer_ids]
    return _graph(result)


def _validate_terms_operations(plan):
    """Terms 以 Offer 主键为身份，存在的 Offer ID 也可插入。"""
    offers = {r["id"] for r in plan["before"].get("offers", [])}
    offers.update(op["id"] for op in plan["operations"]
                  if op["table"] == "promo_offer_master" and op["action"] == "insert")
    existing = {r["offer_id"] for r in plan["before"].get("terms", [])}
    seen = set()
    for op in plan["operations"]:
        if op.get("table") != TERMS:
            continue
        row_id, values = op.get("id"), op.get("values")
        if op.get("action") not in {"insert", "update"}:
            raise WriterError("invalid_terms_action")
        if type(row_id) is not int or row_id == 0 or row_id not in offers:
            raise WriterError("invalid_terms_reference")
        if row_id in seen:
            raise WriterError("duplicate_operation")
        seen.add(row_id)
        if not isinstance(values, dict) or not values or set(values) - TABLES[TERMS][2]:
            raise WriterError("invalid_terms_operation")
        if "offer_id" in values and (type(values["offer_id"]) is not int or values["offer_id"] != row_id):
            raise WriterError("invalid_terms_operation")
        if values.get("membership_plan_id") is not None:
            raise WriterError("ownership_changed")
        if op["action"] == "insert":
            if values.get("offer_id") != row_id or row_id in existing:
                raise WriterError("invalid_terms_reference")
        elif row_id not in existing or row_id < 0:
            raise WriterError("invalid_terms_reference")


def _validate_plan(plan):
    if plan.get("schema_version") != 1 or plan.get("status") != "ready":
        raise WriterError("plan_not_ready")
    source = plan.get("source", {})
    if type(source.get("business_id")) is not int or source["business_id"] <= 0:
        raise WriterError("invalid_source")
    normalize_source_url(source.get("url"))
    if set(plan.get("before", {})) not in (BEFORE_GROUPS, BEFORE_GROUPS | {"terms"}):
        raise WriterError("incomplete_before")
    operations = plan.get("operations")
    if not isinstance(operations, list):
        raise WriterError("invalid_operations")
    seen = set()
    for op in operations:
        table = op.get("table")
        if table not in TABLES:
            raise WriterError("unknown_table" if table else "invalid_operation")
        if table == TERMS:
            continue
        if op.get("action") not in {"insert", "update"}:
            raise WriterError("invalid_operation")
        if type(op.get("id")) is not int or op["id"] == 0 or (op["id"] < 0) != (op["action"] == "insert"):
            raise WriterError("invalid_id")
        if (table, op["id"]) in seen:
            raise WriterError("duplicate_operation")
        seen.add((table, op["id"]))
        values = op.get("values")
        if not isinstance(values, dict) or not values or not set(values) <= TABLES[table][2] - {TABLES[table][1], "created_at", "updated_at", "offer_fingerprint"}:
            raise WriterError("invalid_fields")
    _validate_terms_operations(plan)
    _graph(plan["before"])


def _protected_refs(protections):
    refs = set()
    for key, table in (("offer_ids", "promo_offer_master"), ("item_ids", "promo_offer_items"), ("promotion_ids", "clinic_promotions")):
        refs.update((table, int(value)) for value in protections.get(key, []))
    for entry in protections.get("entries", []):
        if entry.get("table") not in TABLES or type(entry.get("id")) is not int:
            raise WriterError("invalid_protection")
        refs.add((entry["table"], entry["id"]))
    return refs


def _protect(graph, operations, protections):
    """受保护行的上下游（父子触发）在渲染前即拒绝。"""
    refs = _protected_refs(protections)
    offers = {r["id"]: r for r in graph["offers"]}
    items = {r["offer_item_id"]: r for r in graph["items"]}
    for op in operations:
        table, row_id = op["table"], op["id"]
        touches = {(table, row_id)}
        if table == "clinic_promotions":
            # 仅 is_offer_extracted 不触发子行或级联触发器
            children = ([r for r in offers.values() if r.get("promotion_id") == row_id]
                        if set(op["values"]) - {"is_offer_extracted"} else [])
        elif table == "promo_offer_master":
            parent = op["values"].get("promotion_id", offers.get(row_id, {}).get("promotion_id"))
            touches.add(("clinic_promotions", parent))
            children = [offers.get(row_id, {"id": row_id})]
        else:
            parent = row_id if table == TERMS else op["values"].get("offer_id", items.get(row_id, {}).get("offer_id"))
            children = [offers.get(parent, {"id": parent})]
            touches.add(("clinic_promotions", offers.get(parent, {}).get("promotion_id")))
        for child in children:
            touches.add(("promo_offer_master", child["id"]))
            touches.update(("promo_offer_items", i["offer_item_id"]) for i in items.values() if i["offer_id"] == child["id"])
        if touches & refs:
            raise WriterError("protected_row_or_cascade")


def _url_key(column):
    """normalize_source_url 的 SQL 等价式；非 http(s) 或含用户信息的值得到 NULL（不匹配任何来源）。"""
    pattern = URL_KEY_PATTERN
    return (r"(select lower(regexp_replace(km[1], '^www\.', '', 'i'))"
            r" || case when km[2] is not null and km[2] not in ('80', '443') then ':' || km[2] else '' end"
            r" || rtrim(km[3], '/')"
            r" || case when coalesce(km[4], '') <> '' then '?' || km[4] else '' end"
            f" from (select regexp_match(btrim({column}, E' \\t\\r\\n'), {literal(pattern)}) as km) k)")


class _Renderer:
    def __init__(self, plan, protections):
        self.plan = plan
        self.operations = plan["operations"]
        self.source = plan["source"]
        self.business = self.source["business_id"]
        self.key = normalize_source_url(self.source["url"])
        self.before = _graph(plan["before"])
        self.protections = protections
        self.refs = _protected_refs(protections)
        self.variables: dict[tuple[str, int], str] = {}
        self.declared: list[str] = []
        for op in self.operations:
            if op["table"] != TERMS and op["action"] == "insert":
                name = f"{_VARIABLE_PREFIX[op['table']]}_{-op['id']}"
                self.variables[(op["table"], op["id"])] = name
                self.declared.append(f"{name} bigint")
        self.declared += ["v_count bigint", "v_seq text", "v_try integer"]
        self.promotions = (f"select p.promotion_id from public.clinic_promotions p where p.business_id = {self.business} "
                           f"and {_url_key('p.source_url')} = {literal(self.key)}")
        self.offers = f"select m.id from public.promo_offer_master m where m.promotion_id in ({self.promotions})"
        self.items = f"select i.offer_item_id from public.promo_offer_items i where i.offer_id in ({self.offers})"

    def ref(self, table, value):
        if isinstance(value, int) and not isinstance(value, bool) and value < 0:
            name = self.variables.get((table, value))
            if name is None:
                raise WriterError("unresolved_foreign_key")
            return Raw(name)
        return value

    def resolved(self, op):
        """计划值，负数的 promotion_id / offer_id 换成插入后的变量。"""
        values = {}
        for key, value in op["values"].items():
            if key == "promotion_id":
                value = self.ref("clinic_promotions", value)
            elif key == "offer_id":
                value = self.ref("promo_offer_master", value)
            values[key] = value
        return values

    def values(self, op):
        return {key: _write_value(key, value) for key, value in self.resolved(op).items()}

    def row_id(self, op):
        return self.ref("promo_offer_master" if op["table"] == TERMS else op["table"], op["id"])

    def check_plan(self):
        before_ids = {group: {row[pk] for row in self.before[group]} for group, pk in
                      (("promotions", "promotion_id"), ("offers", "id"), ("items", "offer_item_id"))}
        promotion = self.source.get("promotion_id")
        if promotion is not None and promotion not in before_ids["promotions"]:
            raise WriterError("source_id_mismatch")
        terms = {row["offer_id"] for row in self.before.get("terms", [])}
        for op in self.operations:
            if op["action"] != "update":
                continue
            if op["table"] == TERMS:
                inside = op["id"] in terms
            else:
                inside = op["id"] in before_ids[BASE_TABLES[op["table"]][0]]
            if not inside:
                raise WriterError("operation_outside_source")
            group = TABLES[op["table"]][0]
            old = next((r for r in self.before[group] if r[TABLES[op["table"]][1]] == op["id"]), {})
            for key in ("business_id", "promotion_id", "offer_id", "membership_plan_id"):
                if key in op["values"] and op["values"][key] != old.get(key):
                    raise WriterError("ownership_changed")
        _protect(self.before, self.operations, self.protections)

    def lock_statements(self):
        business_offers = (f"select id from public.promo_offer_master where business_id = {self.business} or promotion_id in "
                           f"(select promotion_id from public.clinic_promotions where business_id = {self.business})")
        statements = []
        if any(op["table"] == "clinic_promotions" and op["action"] == "insert" for op in self.operations):
            statements.append("lock table public.clinic_promotions in share row exclusive mode;")
        statements += [
            f"perform 1 from public.clinic_promotions where business_id = {self.business} for update;",
            f"perform 1 from public.promo_offer_master where id in ({business_offers}) for update;",
            f"perform 1 from public.promo_offer_items where offer_id in ({business_offers}) for update;",
            f"perform 1 from public.clinic_services where business_id = {self.business} for update;",
        ]
        if "terms" in self.before or any(op["table"] == TERMS for op in self.operations):
            statements.append(f"perform 1 from public.promo_offer_terms where offer_id in ({business_offers}) for update;")
        return statements

    def stale_statements(self):
        statements = []
        for group, select, label in (("promotions", self.promotions, "promotions"), ("offers", self.offers, "offers"),
                                     ("items", self.items, "items")):
            pk = {"promotions": "promotion_id", "offers": "id", "items": "offer_item_id"}[group]
            ids = [row[pk] for row in self.before[group]]
            statements.append(_raise(
                f"coalesce((select array_agg(x order by x) from ({select}) s(x)), '{{}}'::bigint[]) is distinct from {_ids(ids)}",
                f"stale_before: {label} membership"))
        services = [row["service_id"] for row in self.before["services"]]
        statements.append(_raise(
            f"coalesce((select array_agg(service_id order by service_id) from public.clinic_services "
            f"where business_id = {self.business}), '{{}}'::bigint[]) is distinct from {_ids(services)}",
            "stale_before: services membership"))
        if "terms" in self.before:
            terms = [row["offer_id"] for row in self.before["terms"]]
            statements.append(_raise(
                f"coalesce((select array_agg(t.offer_id order by t.offer_id) from public.promo_offer_terms t "
                f"where t.offer_id in ({self.offers})), '{{}}'::bigint[]) is distinct from {_ids(terms)}",
                "stale_before: terms membership"))
        tables = {"promotions": "clinic_promotions", "offers": "promo_offer_master", "items": "promo_offer_items",
                  "services": "clinic_services", "terms": TERMS}
        for group, rows in self.before.items():
            table = tables[group]
            pk = "service_id" if group == "services" else TABLES[table][1]
            for row in rows:
                statements.append(_raise(f"not {row_matches(table, pk, row)}", f"stale_before: {table} {int(row[pk])}"))
        return statements

    def protected_sets(self):
        by_table = {}
        for table, row_id in self.refs:
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

    def protected_snapshot(self):
        _, sets, expanded_offers, expanded_items = self.protected_sets()
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

    def protection_statements(self):
        if not self.refs:
            return [], []
        by_table, sets, expanded_offers, expanded_items = self.protected_sets()
        pk = {"clinic_promotions": "promotion_id", "promo_offer_master": "id", "promo_offer_items": "offer_item_id", TERMS: "offer_id"}
        statements = []
        for table, ids in sorted(by_table.items()):
            listed = "'{" + ",".join(str(i) for i in sorted(ids)) + "}'::bigint[]"
            statements.append(_raise(
                f"(select count(*) from public.{table} where {pk[table]} = any({listed})) <> {len(set(ids))}",
                f"protected_row_missing: {table}"))
        statements.insert(0, "v_protected := " + self.protected_snapshot() + ";")
        for op in self.operations:
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
        after = _raise(f"{self.protected_snapshot()} is distinct from v_protected", "protected_row_changed")
        return statements, [after]

    def allocation_statements(self):
        statements = []
        previous = None
        for op in self.operations:
            if op["action"] != "insert" or op["table"] != "clinic_promotions":
                continue
            name = self.variables[(op["table"], op["id"])]
            if previous is None:
                statements.append(f"{name} := (select coalesce(max(promotion_id), 0) + 1 from public.clinic_promotions);")
            else:
                statements.append(f"{name} := {previous} + 1;")
            previous = name
        for op in self.operations:
            if op["action"] != "insert" or op["table"] in {"clinic_promotions", TERMS}:
                continue
            table, pk = op["table"], TABLES[op["table"]][1]
            name = self.variables[(table, op["id"])]
            statements.append(
                f"v_seq := pg_get_serial_sequence({literal('public.' + table)}, {literal(pk)});\n"
                f"  if v_seq is null then raise exception 'missing_existing_sequence'; end if;\n"
                f"  v_try := 0;\n"
                f"  loop\n"
                f"    {name} := nextval(v_seq::regclass);\n"
                f"    exit when not exists (select 1 from public.{table} where {pk} = {name});\n"
                f"    v_try := v_try + 1;\n"
                f"    if v_try >= 100 then raise exception 'sequence_lag'; end if;\n"
                f"  end loop;")
        return statements

    def write_statements(self):
        statements = []
        activations = []
        for table in TABLES:
            for op in self.operations:
                if op["table"] != table:
                    continue
                values = self.values(op)
                if table == "promo_offer_master" and (values.get("is_active") is True or op["action"] == "insert"):
                    requested = values.get("is_active", False)
                    values["is_active"] = False
                    activations.append(f"update public.promo_offer_master set is_active = {_sql(requested)} "
                                       f"where id = {self.row_id(op)};")
                    activations.append(self.one_row())
                statements.extend(self.mutation(op, values))
        statements.extend(activations)
        return statements

    def one_row(self):
        return ("get diagnostics v_count = row_count; "
                "if v_count <> 1 then raise exception 'write_not_one_row'; end if;")

    def mutation(self, op, values):
        table = op["table"]
        pk = TABLES[table][1]
        row_id = self.row_id(op)
        if op["action"] == "insert":
            columns = dict(values)
            if table != TERMS:
                columns = {pk: row_id, **columns}
            names = ", ".join(columns)
            rendered = ", ".join(_sql(v) for v in columns.values())
            statement = f"insert into public.{table} ({names}) values ({rendered})"
            if table == TERMS:
                updates = ", ".join(f"{k} = excluded.{k}" for k in columns if k != "offer_id")
                statement += f" on conflict (offer_id) do update set {updates}" if updates else " on conflict (offer_id) do nothing"
            return [statement + ";", self.one_row()]
        assignments = ", ".join(f"{k} = {_sql(v)}" for k, v in values.items())
        return [f"update public.{table} set {assignments} where {pk} = {row_id};", self.one_row()]

    def requested_statements(self):
        statements = []
        for op in self.operations:
            table = op["table"]
            pk = TABLES[table][1]
            values = self.resolved(op)
            row_id = self.row_id(op)
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

    def membership_statements(self):
        inserted = {table: [name for (t, _), name in self.variables.items() if t == table] for table in BASE_TABLES}
        statements = []
        for group, select, pk, table in (("promotions", self.promotions, "promotion_id", "clinic_promotions"),
                                         ("offers", self.offers, "id", "promo_offer_master"),
                                         ("items", self.items, "offer_item_id", "promo_offer_items")):
            expected = [row[pk] for row in self.before[group]] + [Raw(n) for n in inserted[table]]
            statements.append(_raise(
                f"coalesce((select array_agg(x order by x) from ({select}) s(x)), '{{}}'::bigint[]) is distinct from {_ids(expected)}",
                f"source_membership_changed: {group}"))
        for name in inserted["clinic_promotions"]:
            statements.append(_raise(
                f"exists (select 1 from public.clinic_promotions where promotion_id = {name} and business_id is distinct from {self.business})",
                "cross_business_source"))
        return statements

    def row_state_statements(self):
        statements = []
        updates = {(op["table"], op["id"]): op for op in self.operations if op["action"] == "update"}
        for table, (group, pk, _) in BASE_TABLES.items():
            for row in self.before[group]:
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

    def offer_statements(self):
        business = self.business
        blanket = f"coalesce(m.offer_raw_text, '') ~* {literal(BLANKET_REGEX_SQL)}"
        source = f"public.promo_offer_master m where m.promotion_id in ({self.promotions})"
        active = f"m.is_active"
        joined = (f"from public.promo_offer_master m join public.promo_offer_items i on i.offer_id = m.id "
                  f"where m.promotion_id in ({self.promotions}) and m.is_active")

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

    def terms_statements(self):
        if "terms" not in self.before:
            return []
        master = {op["id"] for op in self.operations if op["table"] == "promo_offer_master" and op["id"] > 0}
        requested = {op["id"]: set(op["values"]) for op in self.operations if op["table"] == TERMS and op["id"] > 0}
        statements = []
        for row in self.before["terms"]:
            offer_id = row["offer_id"]
            # 主表 qualification 触发器会同步这些列；Terms 请求自己写的列由 requested_fields 核对
            ignored = {"created_at", "updated_at"} | requested.get(offer_id, set())
            if offer_id in master:
                ignored |= LEGACY_TERMS_COLUMNS
            statements.append(_raise(
                f"not {row_equal_excluding(TERMS, 'offer_id', row, ignored)}",
                f"unexpected_terms_or_trigger_change: {int(offer_id)}"))
        return statements

    def affected(self):
        """计划行及触发器可能连带改动的行（子行、兄弟行、父行）。"""
        ids = {"promotions": set(), "offers": set(), "items": set()}
        offers = {r["id"]: r for r in self.before["offers"]}
        items = {r["offer_item_id"]: r for r in self.before["items"]}

        def touch_offer(offer_id):
            ids["offers"].add(offer_id)
            ids["items"].update(i["offer_item_id"] for i in items.values() if i["offer_id"] == offer_id)
            parent = offers.get(offer_id, {}).get("promotion_id")
            if parent is not None:
                ids["promotions"].add(parent)

        for op in self.operations:
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

    def unaffected_statements(self):
        """其余行（含兄弟 Offer）的版本不得因本批写入或触发器而变化。"""
        affected = self.affected()
        inserted = {group: [Raw(name) for (table, _), name in self.variables.items() if table == BASE_TABLES_BY_GROUP[group]]
                    for group in affected}
        scopes = {"promotions": f"t.promotion_id in ({self.promotions})", "offers": f"t.id in ({self.offers})",
                  "items": f"t.offer_item_id in ({self.items})"}
        statements = []
        for table, (group, pk, _) in BASE_TABLES.items():
            rows = [row for row in self.before[group] if row[pk] not in affected[group]]
            skipped = sorted(affected[group]) + inserted[group]
            statements.append(_raise(versions_differ(table, pk, rows, scopes[group], exclude=skipped),
                                     f"unexpected_row_or_trigger_change: {table}"))
        return statements

    def render(self):
        self.check_plan()
        before_protection, after_protection = self.protection_statements()
        statements = self.lock_statements() + self.stale_statements() + before_protection
        statements += self.allocation_statements() + self.write_statements()
        statements.append("set constraints all immediate;")
        statements += after_protection + self.requested_statements() + self.membership_statements()
        statements += self.row_state_statements() + self.unaffected_statements()
        statements += self.offer_statements() + self.terms_statements()
        statements.append(_raise(f"exists ({BLANKET_CHECK_SQL})", "blanket_service_mapping"))
        counts = {}
        for op in self.operations:
            key = f"{op['table']}.{op['action']}"
            counts[key] = counts.get(key, 0) + 1
        header = (f"monitor price sync apply\nplan sha256 {plan_digest(self.plan)}\nsource {self.source['url']}\n"
                  f"business_id {self.business}\noperations {counts}")
        declared = self.declared + (["v_protected jsonb"] if self.refs else [])
        return do_block(statements, declare=declared, header=header)


def plan_summary(plan):
    counts = {}
    for op in plan.get("operations", []):
        key = f"{op['table']}.{op['action']}"
        counts[key] = counts.get(key, 0) + 1
    source = plan.get("source", {})
    return {"source_url": source.get("url"), "business_id": source.get("business_id"),
            "operations": counts, "review_items": len(plan.get("review", []))}


def render_apply(plan, protections=None):
    """已就绪计划渲染为单个 DO 块：前置核对、写入、写后不变量核对，任一不符 raise 并整体回滚。"""
    _validate_plan(plan)
    return _Renderer(plan, protections or {}).render()
