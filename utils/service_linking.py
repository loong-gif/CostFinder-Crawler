"""从 offer 识别 service，回填 clinic_services 与 promo_offer_items.service_id（标准流程第 6 段）。

由快照生成冻结计划；渲染为单个 DO 块执行，结束前运行 AGENTS.md 的 blanket 检查。
"""
from __future__ import annotations

import json
import re
from collections import defaultdict
from datetime import datetime, timezone
from decimal import Decimal
from functools import lru_cache
from typing import Any, Mapping, Protocol

from utils.clinic_service_extraction import _normalize_unit_type, infer_service_name_for_item
from utils.clinic_service_taxonomy import (
    _SERVICE_NAME_ENUMS,
    SERVICE_NAME_ENUM_VALUES,
    _normalized_name,
    live_clinic_service_payload,
    service_name_enum_hint,
)
from utils.offer_extraction_llm import _service_dictionary_indexes
from utils.pipeline_db import SERVICE_NAME_ENUM_SNAPSHOT, do_block, literal, plan_digest, render
from utils.promo_offer_items_db import _BLANKET_OFFER_RE, AGENTS_BLANKET_PATTERN, is_blanket_offer_text
from utils.service_price_guard import normalize_source_url

SNAPSHOT_TABLES = {
    "items": ("promo_offer_items", "offer_item_id, offer_id, item_name, service_id, delivered_units, unit_price", "true"),
    "offers": ("promo_offer_master", "id, business_id, offer_raw_text, promotion_id, membership_plan_id, "
               "is_package, discount_price", "true"),
    "services": ("clinic_services", "service_id, business_id, service_name::text as service_name, unit_type, "
                 "service_area", "true"),
    "promotions": ("clinic_promotions", "promotion_id, source_url, is_offer_extracted", "true"),
    "memberships": ("clinic_memberships", "plan_id, source_url, is_offer_extracted", "true"),
    "enums": SERVICE_NAME_ENUM_SNAPSHOT,
}

# is_blanket_offer_text 的 SQL 等价形式：AGENTS.md 的正则原文，加上 promo_offer_items_db 中的其余 blanket 词（\b 对应 \y）
BLANKET_SQL_PATTERN = f"({AGENTS_BLANKET_PATTERN})|" + _BLANKET_OFFER_RE.pattern.replace(r"\b", r"\y")

LLM_BATCH_SIZE = 25
LLM_NONE = "NONE"
LLM_MULTIPLE = "MULTIPLE"

# 通用服务名被同族具体名称覆盖时，只保留具体名称
SUBSUMED_BY = {
    "Neurotoxin": {"Botox", "Dysport", "Xeomin", "Jeuveau", "Daxxify", "Letybo", "Myobloc"},
    "Dermal Filler": {"Juvéderm", "Restylane", "Radiesse", "Belotero", "RHA Filler", "Skinvive", "Versa",
                      "Evolysse", "EZ Gel", "Sculptra"},
    "Microneedling": {"Morpheus8", "RF Microneedling", "SkinPen Microneedling", "Sylfirm X", "Microchanneling"},
    "Facial": {"Hydrafacial", "DiamondGlow", "Glo2 Facial", "Customized Facial", "Dermaplaning"},
    "Chemical Peel": {"VI Peel", "Lunchtime Peel", "PRX Skin Perfecting", "Perfect Derma Peel", "CoolPeel"},
    "Laser Treatment": {"CO2 Laser", "Halo Laser", "Moxi Laser", "Picoway", "Vbeam", "ClearSilk", "Laser Genesis",
                        "Fraxel", "Clear + Brilliant", "Pearl Fractional"},
    "Skin Tightening": {"Ultherapy", "Thermage", "HIFU", "Sofwave", "Tixel", "Forma", "Morpheus8"},
    "Body Contouring": {"CoolSculpting", "Emsculpt", "Lipo Dissolve"},
}

UNIT_TYPE_WORDS = {"unit": "unit", "units": "unit", "syringe": "syringe", "syringes": "syringe",
                   "vial": "vial", "vials": "vial", "session": "session", "sessions": "session",
                   "area": "area", "areas": "area", "shot": "shot", "shots": "shot"}

# AGENTS.md 规定的批次检查原文；apply 时追加条件，只检查本批关联的 item（存量的有意保留行由 batch-gate 登记）
BLANKET_CHECK_SQL = """
select m.id, m.offer_raw_text, i.item_name
from promo_offer_master m
join promo_offer_items i on i.offer_id = m.id
where m.is_active
  and i.service_id is not null
  and m.offer_raw_text ~* '(all services|any service|all products|all treatments|gift card|skin ?care products)'
"""


class StructuredClient(Protocol):
    def create_json_response(self, messages: list[dict[str, str]], *, json_schema: dict[str, Any] | None = None
                             ) -> dict[str, Any]: ...


@lru_cache(maxsize=1)
def _scan_keys() -> list[tuple[str, str]]:
    """别名与枚举名（规范化后）到枚举值的映射，按键长度降序。"""
    keys: dict[str, str] = {key: value for key, value in _SERVICE_NAME_ENUMS.items() if key}
    for alias, canonical in _service_dictionary_indexes()["alias_by_normalized"].items():
        hint = service_name_enum_hint(canonical)
        key = _normalized_name(alias)
        if hint is not None and key:
            keys.setdefault(key, hint)
    return sorted(keys.items(), key=lambda pair: len(pair[0]), reverse=True)


@lru_cache(maxsize=1)
def _scan_patterns() -> list[tuple[re.Pattern[str], str]]:
    return [(re.compile(rf"(?<![a-z0-9]){re.escape(key)}(?![a-z0-9])"), value) for key, value in _scan_keys()]


def scan_services(text: Any) -> set[str]:
    """文本中出现的全部枚举服务名；长键优先，已匹配区间不再重复匹配，通用名被同族具体名覆盖。"""
    normalized = _normalized_name(text)
    if not normalized:
        return set()
    consumed = [False] * len(normalized)
    found: set[str] = set()
    for pattern, value in _scan_patterns():
        for match in pattern.finditer(normalized):
            if any(consumed[match.start():match.end()]):
                continue
            for index in range(match.start(), match.end()):
                consumed[index] = True
            found.add(value)
    return {name for name in found if not (SUBSUMED_BY.get(name, set()) & found)}


def item_unit_type(item_name: Any) -> str | None:
    """item_name 中可确认的计量单位；出现多种不同单位时不确认。"""
    words = re.findall(r"[a-z]+", str(item_name or "").casefold())
    found = {UNIT_TYPE_WORDS[word] for word in words if word in UNIT_TYPE_WORDS}
    return _normalize_unit_type(next(iter(found))) if len(found) == 1 else None


def recognize_deterministic(*, item_name: Any, offer_raw_text: str, quantity: Any, sibling_count: int,
                            is_package: bool) -> dict[str, Any]:
    """确定性识别。返回 {service_name, basis, reason}；basis 为 deterministic / needs_llm / unresolved。"""
    in_item = scan_services(item_name)
    if len(in_item) == 1:
        return {"service_name": next(iter(in_item)), "basis": "deterministic", "reason": "item_name"}
    if len(in_item) > 1:
        return {"service_name": None, "basis": "unresolved", "reason": "multiple_services_in_item",
                "candidates": sorted(in_item)}
    if sibling_count > 1 or is_package:
        return {"service_name": None, "basis": "needs_llm", "reason": "no_match_in_multi_item_offer"}
    in_text = scan_services(offer_raw_text)
    if len(in_text) == 1:
        return {"service_name": next(iter(in_text)), "basis": "deterministic", "reason": "offer_raw_text"}
    if len(in_text) > 1:
        return {"service_name": None, "basis": "unresolved", "reason": "multiple_services_in_offer_text",
                "candidates": sorted(in_text)}
    inferred = service_name_enum_hint(infer_service_name_for_item(
        offer_raw_text=offer_raw_text, quantity=quantity, item_name=item_name,
        sibling_count=sibling_count, is_package=is_package))
    if inferred is not None and inferred != "Others":
        return {"service_name": inferred, "basis": "deterministic", "reason": "quantity_heuristic"}
    return {"service_name": None, "basis": "needs_llm", "reason": "no_match"}


class LlmServiceResolver:
    """用 LLM 在数据库枚举值内为单个 item 选择服务；多个服务或无法判定时返回 NONE / MULTIPLE。"""

    def __init__(self, client: StructuredClient, enum_values: list[str]) -> None:
        self.client = client
        self.enum_values = list(enum_values)
        self.schema = {
            "type": "object",
            "additionalProperties": False,
            "required": ["results"],
            "properties": {"results": {"type": "array", "items": {
                "type": "object",
                "additionalProperties": False,
                "required": ["index", "service_name"],
                "properties": {"index": {"type": "integer"},
                               "service_name": {"type": "string", "enum": self.enum_values + [LLM_NONE, LLM_MULTIPLE]}},
            }}},
        }

    def resolve(self, requests: list[dict[str, Any]]) -> dict[int, str]:
        answers: dict[int, str] = {}
        for start in range(0, len(requests), LLM_BATCH_SIZE):
            batch = requests[start:start + LLM_BATCH_SIZE]
            messages = [
                {"role": "system", "content": (
                    "You map medical-aesthetics promotion items to exactly one service_name from the allowed enum. "
                    f"Answer {LLM_MULTIPLE} when the item bundles several different services, and {LLM_NONE} "
                    "when no allowed service is clearly identified. Use only the given text.")},
                {"role": "user", "content": json.dumps(batch, ensure_ascii=False)},
            ]
            payload = self.client.create_json_response(messages, json_schema=self.schema)
            results = payload["results"]
            returned = sorted(entry["index"] for entry in results)
            if returned != sorted(request["index"] for request in batch):
                raise RuntimeError(f"llm_index_mismatch: expected {[r['index'] for r in batch]}, got {returned}")
            for entry in results:
                if entry["service_name"] not in self.enum_values + [LLM_NONE, LLM_MULTIPLE]:
                    raise RuntimeError(f"llm_invalid_service_name: {entry['service_name']}")
                answers[entry["index"]] = entry["service_name"]
        return answers


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
    linked_item_ids = set()
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
        linked_item_ids.add(item["offer_item_id"])

    def complete(owner_offers: list[dict[str, Any]]) -> bool:
        relevant = [i for o in owner_offers if not is_blanket_offer_text(o["offer_raw_text"])
                    for i in items_by_offer[o["id"]]]
        return bool(relevant) and all(i["service_id"] is not None or i["offer_item_id"] in linked_item_ids
                                      for i in relevant)

    promotion_offers: dict[int, list[dict[str, Any]]] = defaultdict(list)
    membership_offers: dict[int, list[dict[str, Any]]] = defaultdict(list)
    for offer in offers:
        if offer.get("promotion_id") is not None:
            promotion_offers[offer["promotion_id"]].append(offer)
        if offer.get("membership_plan_id") is not None:
            membership_offers[offer["membership_plan_id"]].append(offer)
    return {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "counts": {action: sum(1 for e in plan_items if e["action"] == action)
                   for action in ("link", "create_and_link", "unresolved")},
        "services_to_create": list(to_create.values()),
        "items": plan_items,
        "promotions_to_mark": sorted(p["promotion_id"] for p in promotions if not p["is_offer_extracted"]
                                     and complete(promotion_offers.get(p["promotion_id"], []))),
        "memberships_to_mark": sorted(m["plan_id"] for m in memberships if not m["is_offer_extracted"]
                                      and complete(membership_offers.get(m["plan_id"], []))),
    }


def build_plan(snapshot: Mapping[str, list[dict[str, Any]]], client: StructuredClient | None) -> dict[str, Any]:
    enum_values = [row["enumlabel"] for row in sorted(snapshot["enums"], key=lambda row: row["enumsortorder"])]
    resolver = LlmServiceResolver(client, enum_values) if client is not None else None
    return build_plan_from_rows(
        items=snapshot["items"], offers=snapshot["offers"], services=snapshot["services"],
        promotions=snapshot["promotions"], memberships=snapshot["memberships"],
        enum_values=enum_values, resolver=resolver)


def _create_service_statements(index: int, spec: dict[str, Any]) -> list[str]:
    """复用计划之后已出现的唯一同名行；没有则新建（regular_price 为 NULL），出现多行即视为计划过期。"""
    variable = f"v_service_{index}"
    payload = live_clinic_service_payload({
        "business_id": spec["business_id"], "service_name": spec["service_name"], "regular_price": None,
        "unit_type": spec["unit_type"], "source_url": spec["source_url"]})
    stale = literal(f"stale_plan: business {spec['business_id']} now has several {spec['service_name']} rows")
    insert = render(
        "insert into public.clinic_services (business_id, service_name, regular_price, unit_type, source_url, "
        "updated_at) values ({business_id}, {service_name}::clinic_service_name_enum, {regular_price}, "
        "{unit_type}, {source_url}, now()) returning service_id into " + variable + ";", **payload)
    return [
        render("select coalesce(array_agg(service_id), '{{}}'::bigint[]) into v_ids from public.clinic_services "
               "where business_id = {business_id} and service_name = {service_name}::clinic_service_name_enum;",
               business_id=spec["business_id"], service_name=spec["service_name"]),
        f"if cardinality(v_ids) > 1 then raise exception {stale}; "
        f"elsif cardinality(v_ids) = 1 then {variable} := v_ids[1]; "
        f"else {insert} end if;",
    ]


def _link_statements(entry: dict[str, Any], service_ref: str) -> list[str]:
    """逐条关联：只更新 service_id 仍为空且服务属于同一商家的 item，恰好 1 行。"""
    item = literal(entry["offer_item_id"])
    failure = literal(f"stale_plan: item {entry['offer_item_id']} cannot be linked to its service")
    return [
        f"update public.promo_offer_items i set service_id = {service_ref}, updated_at = now() "
        f"from public.promo_offer_master m, public.clinic_services s "
        f"where i.offer_item_id = {item} and i.service_id is null and m.id = i.offer_id "
        f"and s.service_id = {service_ref} and s.business_id = m.business_id;",
        "get diagnostics v_count = row_count;",
        f"if v_count <> 1 then raise exception {failure}; end if;",
    ]


def _mark_statements(table: str, id_column: str, owner_column: str, owner_id: int) -> list[str]:
    """is_offer_extracted 置 true 之前，该条记录下遗留的未关联 item 必须全部属于 blanket offer。"""
    owner = literal(owner_id)
    incomplete = literal(f"incomplete_mapping: {table} {owner_id} still has unlinked items")
    missing = literal(f"stale_plan: {table} {owner_id} no longer exists")
    return [
        f"select count(*) into v_count from public.promo_offer_items i join public.promo_offer_master m "
        f"on m.id = i.offer_id where m.{owner_column} = {owner} and i.service_id is null "
        f"and not (coalesce(m.offer_raw_text, '') ~* {literal(BLANKET_SQL_PATTERN)});",
        f"if v_count <> 0 then raise exception {incomplete}; end if;",
        f"update public.{table} set is_offer_extracted = true, updated_at = now() where {id_column} = {owner};",
        "get diagnostics v_count = row_count;",
        f"if v_count <> 1 then raise exception {missing}; end if;",
    ]


def render_apply(plan: Mapping[str, Any]) -> str:
    """整个计划渲染为单个 DO 块；任一 item 已变化、标记条件不成立或 blanket 检查非零即整体回滚。"""
    statements: list[str] = []
    declare = ["v_count bigint", "v_ids bigint[]"]
    used_keys = {i["create_key"] for i in plan["items"] if i["action"] == "create_and_link"}
    variables: dict[str, str] = {}
    for spec in plan["services_to_create"]:
        if spec["key"] not in used_keys:
            continue
        variables[spec["key"]] = f"v_service_{len(variables)}"
        declare.append(f"{variables[spec['key']]} bigint")
        statements.extend(_create_service_statements(len(variables) - 1, spec))
    linked: list[int] = []
    for entry in plan["items"]:
        if entry["action"] == "unresolved":
            continue
        service_ref = literal(entry["service_id"]) if entry["action"] == "link" else variables[entry["create_key"]]
        statements.extend(_link_statements(entry, service_ref))
        linked.append(entry["offer_item_id"])
    for table, id_column, owner_column, ids in (
            ("clinic_promotions", "promotion_id", "promotion_id", plan["promotions_to_mark"]),
            ("clinic_memberships", "plan_id", "membership_plan_id", plan["memberships_to_mark"])):
        for owner_id in ids:
            statements.extend(_mark_statements(table, id_column, owner_column, owner_id))
    # 本批 blanket 检查：AGENTS.md 的查询原文，只检查本批关联的 item
    statements.append(
        f"select count(*) into v_count from ({BLANKET_CHECK_SQL.strip()}\n"
        f"  and i.offer_item_id = any({literal(linked)}::bigint[])) blanket_rows;")
    statements.append(f"if v_count <> 0 then raise exception {literal('blanket_check_failed: linked items under blanket offers')}; end if;")
    header = (f"link-services apply\nplan sha256 {plan_digest(plan)}\n"
              f"links {len(linked)}, services {len(variables)}, promotions {len(plan['promotions_to_mark'])}, "
              f"memberships {len(plan['memberships_to_mark'])}")
    return do_block(statements, declare=declare, header=header)


def dump_plan(plan: dict[str, Any]) -> str:
    return json.dumps(plan, ensure_ascii=False, indent=2, default=str)
