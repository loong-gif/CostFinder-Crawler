"""从 offer 识别 service，回填 clinic_services 与 promo_offer_items.service_id（标准流程第 6 段）。

由快照生成冻结计划；渲染为单个 DO 块执行，结束前运行 AGENTS.md 的 blanket 检查。
"""
from __future__ import annotations

import json
from collections import defaultdict
from datetime import datetime, timezone
from decimal import Decimal
from typing import Any, Mapping

from utils.clinic_service_taxonomy import SERVICE_NAME_ENUM_VALUES, _normalized_name, live_clinic_service_payload
from utils.pipeline_db import SERVICE_NAME_ENUM_SNAPSHOT, do_block, literal, plan_digest
from utils.promo_offer_items_db import is_blanket_offer_text
from utils.service_price_guard import normalize_source_url
from utils.service_recognition import (
    LLM_MULTIPLE,
    LLM_NONE,
    LlmServiceResolver,
    StructuredClient,
    item_unit_type,
    recognize_deterministic,
)

def snapshot_tables(business_ids: list[int] | None = None) -> dict[str, tuple[str, str, str]]:
    """快照表定义；给出 business_ids 时只读取这些商家的 offer、item、service 与来源（只处理这些商家）。"""
    where = "true" if not business_ids else f"business_id = any({literal(sorted(set(business_ids)))}::bigint[])"
    offer_ids = f"offer_id in (select id from public.promo_offer_master where {where})"
    return {
        "items": ("promo_offer_items", "offer_item_id, offer_id, item_name, service_id, delivered_units, unit_price",
                  "true" if not business_ids else offer_ids),
        "offers": ("promo_offer_master", "id, business_id, offer_raw_text, promotion_id, membership_plan_id, "
                   "is_package, discount_price", where),
        "services": ("clinic_services", "service_id, business_id, service_name::text as service_name, unit_type, "
                     "service_area", where),
        "promotions": ("clinic_promotions", "promotion_id, source_url", where),
        "memberships": ("clinic_memberships", "plan_id, source_url", where),
        "enums": SERVICE_NAME_ENUM_SNAPSHOT,
    }


SNAPSHOT_TABLES = snapshot_tables()

# AGENTS.md 规定的批次检查原文；apply 时追加条件，只检查本批关联的 item（存量的有意保留行由 batch-gate 登记）
BLANKET_CHECK_SQL = """
select m.id, m.offer_raw_text, i.item_name
from promo_offer_master m
join promo_offer_items i on i.offer_id = m.id
where m.is_active
  and i.service_id is not null
  and m.offer_raw_text ~* '(all services|any service|all products|all treatments|gift card|skin ?care products)'
"""


def match_service_row(rows: list[dict[str, Any]], unit_type: str | None, text: Any) -> tuple[str, dict[str, Any] | None]:
    """在同商家同 service_name 的行中唯一匹配：none / one / ambiguous。"""
    if not rows:
        return "none", None
    if len(rows) == 1:
        return "one", rows[0]
    if unit_type is not None:
        by_unit = [row for row in rows if row.get("unit_type") == unit_type]
        if len(by_unit) == 1:
            return "one", by_unit[0]
    normalized = _normalized_name(text)
    by_area = [row for row in rows if row.get("service_area") and _normalized_name(row["service_area"]) in normalized]
    if len(by_area) == 1:
        return "one", by_area[0]
    return "ambiguous", None


def price_invariant_ok(offer: dict[str, Any], item: dict[str, Any], item_count: int) -> bool:
    """数据库延迟约束：非 package 单 item offer 的 discount_price 必须等于 unit_price * 数量。"""
    if offer.get("is_package") or item_count != 1 or offer.get("discount_price") is None:
        return True
    if item.get("unit_price") is None:
        return False
    quantity = item.get("delivered_units")
    quantity = Decimal(1) if quantity is None else Decimal(str(quantity))
    return round(Decimal(str(item["unit_price"])) * quantity, 2) == round(Decimal(str(offer["discount_price"])), 2)


def build_plan_from_rows(*, items: list[dict[str, Any]], offers: list[dict[str, Any]],
                         services: list[dict[str, Any]], promotions: list[dict[str, Any]],
                         memberships: list[dict[str, Any]], enum_values: list[str],
                         resolver: LlmServiceResolver | None) -> dict[str, Any]:
    if set(enum_values) != set(SERVICE_NAME_ENUM_VALUES):
        raise RuntimeError("enum_mismatch: clinic_service_name_enum differs from SERVICE_NAME_ENUM_VALUES")
    offer_by_id = {offer["id"]: offer for offer in offers}
    items_by_offer: dict[int, list[dict[str, Any]]] = defaultdict(list)
    for item in items:
        items_by_offer[item["offer_id"]].append(item)
    source_urls = {("promotion", p["promotion_id"]): p["source_url"] for p in promotions}
    source_urls.update({("membership", m["plan_id"]): m["source_url"] for m in memberships})
    rows_by_key: dict[tuple[int, str], list[dict[str, Any]]] = defaultdict(list)
    for service in services:
        rows_by_key[(service["business_id"], service["service_name"])].append(dict(service))

    pending = sorted((i for i in items if i["service_id"] is None), key=lambda i: i["offer_item_id"])
    decisions: dict[int, dict[str, Any]] = {}
    llm_requests = []
    for item in pending:
        offer = offer_by_id[item["offer_id"]]
        if is_blanket_offer_text(offer["offer_raw_text"]):
            decisions[item["offer_item_id"]] = {"service_name": None, "basis": "blanket", "reason": "blanket_offer"}
            continue
        decision = recognize_deterministic(
            item_name=item["item_name"], offer_raw_text=offer["offer_raw_text"] or "",
            quantity=item["delivered_units"], sibling_count=len(items_by_offer[item["offer_id"]]),
            is_package=bool(offer.get("is_package")))
        decisions[item["offer_item_id"]] = decision
        if decision["basis"] == "needs_llm":
            llm_requests.append({"index": item["offer_item_id"], "item_name": item["item_name"],
                                 "offer_raw_text": offer["offer_raw_text"]})
    if llm_requests:
        if resolver is None:
            raise RuntimeError("llm_required: items need LLM recognition but no resolver was provided")
        for index, answer in resolver.resolve(llm_requests).items():
            if answer == LLM_NONE:
                decisions[index] = {"service_name": None, "basis": "unresolved", "reason": "llm_none"}
            elif answer == LLM_MULTIPLE:
                decisions[index] = {"service_name": None, "basis": "unresolved", "reason": "llm_multiple_services"}
            else:
                decisions[index] = {"service_name": answer, "basis": "llm", "reason": "llm"}

    to_create: dict[tuple[int, str, str | None], dict[str, Any]] = {}
    taken: dict[int, set[tuple[Any, ...]]] = {
        offer_id: {("id", i["service_id"]) for i in offer_items if i["service_id"] is not None}
        for offer_id, offer_items in items_by_offer.items()}
    plan_items = []
    for item in pending:
        decision = decisions[item["offer_item_id"]]
        offer = offer_by_id[item["offer_id"]]
        business_id = offer["business_id"]
        entry = {"offer_item_id": item["offer_item_id"], "offer_id": item["offer_id"], "business_id": business_id,
                 "item_name": item["item_name"], "recognized": decision["service_name"],
                 "basis": decision["basis"], "reason": decision["reason"], "action": "unresolved",
                 "service_id": None, "create_key": None}
        if decision.get("candidates"):
            entry["candidates"] = decision["candidates"]
        plan_items.append(entry)
        name = decision["service_name"]
        if name is None:
            continue
        if not price_invariant_ok(offer, item, len(items_by_offer[item["offer_id"]])):
            entry["reason"] = "price_invariant_would_fail"
            continue
        unit_type = item_unit_type(item["item_name"])
        kind, row = match_service_row(rows_by_key[(business_id, name)], unit_type, item["item_name"])
        if kind == "ambiguous":
            entry["reason"] = "multiple_service_rows"
            continue
        if kind == "one":
            token = ("id", row["service_id"]) if row.get("service_id") is not None else ("new", row["create_key"])
            action_fields = ({"action": "link", "service_id": row["service_id"]} if row.get("service_id") is not None
                             else {"action": "create_and_link", "create_key": row["create_key"]})
        else:
            source_url = normalize_source_url(source_urls.get(
                ("promotion", offer["promotion_id"]) if offer.get("promotion_id") is not None
                else ("membership", offer.get("membership_plan_id"))) or "")
            if not source_url:
                entry["reason"] = "no_source_url"
                continue
            key = f"{business_id}:{name}:{unit_type}"
            to_create[(business_id, name, unit_type)] = {
                "key": key, "business_id": business_id, "service_name": name,
                "regular_price": None, "unit_type": unit_type, "source_url": source_url}
            virtual = {"service_id": None, "create_key": key, "unit_type": unit_type, "service_area": None}
            rows_by_key[(business_id, name)].append(virtual)
            token = ("new", key)
            action_fields = {"action": "create_and_link", "create_key": key}
        if token in taken[item["offer_id"]]:
            entry["reason"] = "duplicate_service_in_offer"
            continue
        taken[item["offer_id"]].add(token)
        entry.update(action_fields)
    return {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "counts": {action: sum(1 for e in plan_items if e["action"] == action)
                   for action in ("link", "create_and_link", "unresolved")},
        "services_to_create": list(to_create.values()),
        "items": plan_items,
    }


def build_plan(snapshot: Mapping[str, list[dict[str, Any]]], client: StructuredClient | None) -> dict[str, Any]:
    enum_values = [row["enumlabel"] for row in sorted(snapshot["enums"], key=lambda row: row["enumsortorder"])]
    resolver = LlmServiceResolver(client, enum_values) if client is not None else None
    return build_plan_from_rows(
        items=snapshot["items"], offers=snapshot["offers"], services=snapshot["services"],
        promotions=snapshot["promotions"], memberships=snapshot["memberships"],
        enum_values=enum_values, resolver=resolver)


def _json_literal(rows: list[list[Any]]) -> str:
    return literal(json.dumps(rows, ensure_ascii=False)) + "::jsonb"


def _create_service_statements(specs: list[dict[str, Any]]) -> list[str]:
    """逐个 service 规格：复用计划之后已出现的唯一同名行；没有则新建（regular_price 为 NULL），出现多行即视为计划过期。
    新建或复用的 service_id 按 create_key 记入 v_map。"""
    rows = []
    for spec in specs:
        payload = live_clinic_service_payload({
            "business_id": spec["business_id"], "service_name": spec["service_name"], "regular_price": None,
            "unit_type": spec["unit_type"], "source_url": spec["source_url"]})
        rows.append([spec["key"], payload["business_id"], payload["service_name"], payload.get("unit_type"),
                     payload.get("source_url")])
    return [
        "for r in select e->>0 as key, (e->>1)::bigint as business_id, e->>2 as service_name, e->>3 as unit_type, "
        f"e->>4 as source_url from jsonb_array_elements({_json_literal(rows)}) e loop",
        "select coalesce(array_agg(service_id), '{}'::bigint[]) into v_ids from public.clinic_services "
        "where business_id = r.business_id and service_name = r.service_name::clinic_service_name_enum;",
        "if cardinality(v_ids) > 1 then raise exception 'stale_plan: business % now has several % rows', "
        "r.business_id, r.service_name; "
        "elsif cardinality(v_ids) = 1 then v_service := v_ids[1]; "
        "else insert into public.clinic_services (business_id, service_name, regular_price, unit_type, source_url, "
        "updated_at) values (r.business_id, r.service_name::clinic_service_name_enum, NULL, r.unit_type, "
        "r.source_url, now()) returning service_id into v_service; end if;",
        "v_map := v_map || jsonb_build_object(r.key, v_service);",
        "end loop;",
    ]


def _link_statements(entries: list[dict[str, Any]]) -> list[str]:
    """逐条关联：只更新 service_id 仍为空且服务属于同一商家的 item，恰好 1 行。
    已有 service 以数字 id 给出，新建的以 create_key 给出（从 v_map 取）。"""
    rows = [[entry["offer_item_id"],
             str(entry["service_id"]) if entry["action"] == "link" else entry["create_key"]] for entry in entries]
    return [
        "for r in select (e->>0)::bigint as item_id, e->>1 as ref "
        f"from jsonb_array_elements({_json_literal(rows)}) e loop",
        "v_ref := case when r.ref ~ '^[0-9]+$' then r.ref::bigint else (v_map->>r.ref)::bigint end;",
        "update public.promo_offer_items i set service_id = v_ref, updated_at = now() "
        "from public.promo_offer_master m, public.clinic_services s "
        "where i.offer_item_id = r.item_id and i.service_id is null and m.id = i.offer_id "
        "and s.service_id = v_ref and s.business_id = m.business_id;",
        "get diagnostics v_count = row_count;",
        "if v_count <> 1 then raise exception 'stale_plan: item % cannot be linked to its service', r.item_id; end if;",
        "end loop;",
    ]


def render_apply(plan: Mapping[str, Any]) -> str:
    """整个计划渲染为单个 DO 块；任一 item 已变化或 blanket 检查非零即整体回滚。"""
    statements: list[str] = []
    declare = ["v_count bigint", "v_ids bigint[]", "v_service bigint", "v_ref bigint", "v_map jsonb := '{}'::jsonb",
               "r record"]
    used_keys = {i["create_key"] for i in plan["items"] if i["action"] == "create_and_link"}
    specs = [spec for spec in plan["services_to_create"] if spec["key"] in used_keys]
    if specs:
        statements.extend(_create_service_statements(specs))
    entries = [entry for entry in plan["items"] if entry["action"] != "unresolved"]
    if entries:
        statements.extend(_link_statements(entries))
    linked = [entry["offer_item_id"] for entry in entries]
    # 本批 blanket 检查：AGENTS.md 的查询原文，只检查本批关联的 item
    statements.append(
        f"select count(*) into v_count from ({BLANKET_CHECK_SQL.strip()}\n"
        f"  and i.offer_item_id = any({literal(linked)}::bigint[])) blanket_rows;")
    statements.append(f"if v_count <> 0 then raise exception {literal('blanket_check_failed: linked items under blanket offers')}; end if;")
    header = (f"link-services apply\nplan sha256 {plan_digest(plan)}\n"
              f"links {len(linked)}, services {len(specs)}")
    return do_block(statements, declare=declare, header=header)


def dump_plan(plan: dict[str, Any]) -> str:
    return json.dumps(plan, ensure_ascii=False, indent=2, default=str)
