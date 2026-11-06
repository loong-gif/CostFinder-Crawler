"""把已就绪的 monitor 计划渲染为单个 DO 块（pending SQL）；Python 不连库、不写库。

DO 块先在 SQL 中核对业务图快照、保护项与字段白名单，再写入，写后核对不变量，任一不符 raise 并整体回滚。
"""
from __future__ import annotations

from utils.monitor_price_writer_checks import (
    membership_statements, offer_statements, protection_statements, requested_statements,
    row_state_statements, stale_statements, terms_statements, unaffected_statements,
)
from utils.monitor_price_writer_plan import (
    BASE_TABLES, BLANKET_CHECK_SQL, TABLES, TERMS, VARIABLE_PREFIX, WriterError, _graph, _protect,
    normalize_source_url, protected_refs, url_key, validate_plan,
)
from utils.pipeline_db import do_block, literal, plan_digest
from utils.sql_checks import Raw, raise_if as _raise, sql_value as _sql, write_value as _write_value


class _Renderer:
    def __init__(self, plan, protections):
        self.plan = plan
        self.operations = plan["operations"]
        self.source = plan["source"]
        self.business = self.source["business_id"]
        self.key = normalize_source_url(self.source["url"])
        self.before = _graph(plan["before"])
        self.protections = protections
        self.refs = protected_refs(protections)
        self.variables: dict[tuple[str, int], str] = {}
        self.declared: list[str] = []
        for op in self.operations:
            if op["table"] != TERMS and op["action"] == "insert":
                name = f"{VARIABLE_PREFIX[op['table']]}_{-op['id']}"
                self.variables[(op["table"], op["id"])] = name
                self.declared.append(f"{name} bigint")
        self.declared += ["v_count bigint", "v_seq text", "v_try integer"]
        self.promotions = (f"select p.promotion_id from public.clinic_promotions p where p.business_id = {self.business} "
                           f"and {url_key('p.source_url')} = {literal(self.key)}")
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

    def render(self):
        self.check_plan()
        before_protection, after_protection = protection_statements(self)
        statements = self.lock_statements() + stale_statements(self) + before_protection
        statements += self.allocation_statements() + self.write_statements()
        statements.append("set constraints all immediate;")
        statements += after_protection + requested_statements(self) + membership_statements(self)
        statements += row_state_statements(self) + unaffected_statements(self)
        statements += offer_statements(self) + terms_statements(self)
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
    validate_plan(plan)
    return _Renderer(plan, protections or {}).render()
