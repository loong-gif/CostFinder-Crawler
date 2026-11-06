from __future__ import annotations

from datetime import datetime
from urllib.parse import urlsplit

from utils.pipeline_db import literal
from utils.promo_offer_items_db import AGENTS_BLANKET_PATTERN, _BLANKET_OFFER_RE
from utils.schema_contract import (
    CLINIC_PROMOTION_COLUMNS, OFFER_ITEM_COLUMNS, OFFER_MASTER_COLUMNS,
)
from utils.sql_checks import wire as _wire

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
VARIABLE_PREFIX = {"clinic_promotions": "v_promotion", "promo_offer_master": "v_offer", "promo_offer_items": "v_item"}


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


def validate_plan(plan):
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


def protected_refs(protections):
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
    refs = protected_refs(protections)
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


def url_key(column):
    """normalize_source_url 的 SQL 等价式；非 http(s) 或含用户信息的值得到 NULL（不匹配任何来源）。"""
    pattern = URL_KEY_PATTERN
    return (r"(select lower(regexp_replace(km[1], '^www\.', '', 'i'))"
            r" || case when km[2] is not null and km[2] not in ('80', '443') then ':' || km[2] else '' end"
            r" || rtrim(km[3], '/')"
            r" || case when coalesce(km[4], '') <> '' then '?' || km[4] else '' end"
            f" from (select regexp_match(btrim({column}, E' \\t\\r\\n'), {literal(pattern)}) as km) k)")
