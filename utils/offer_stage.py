"""第 5 段：clinic_promotions.promotion_content / clinic_memberships.benefits 抽取为 promo_offer_master + promo_offer_items。

读取：snapshot_tables 生成只读快照查询；计划基于快照生成并冻结为 JSON；
应用：render_apply 渲染单个 DO 块，逐源核对源行与已有 offer 的 updated_at 及 item id 集合未变化后写入，
写后核对 is_active 与预期一致，块末执行 AGENTS.md 的 blanket 检查，任一不符即 raise exception 整体回滚。
服务映射（service_id）由第 6 段负责。
"""
from __future__ import annotations

import json
from datetime import datetime, timezone
from decimal import Decimal
from pathlib import Path
from typing import Any, Mapping

from utils.offer_stage_extract import extract_source
from utils.offer_stage_reconcile import plan_source
from utils.offer_stage_spec import (
    ITEM_COLUMNS, MEMBERSHIPS, OFFER_COLUMNS, PROMOTIONS, SOURCE_SPEC, source_evidence)
from utils.offer_stage_sql import _id_array
from utils.pipeline_db import SERVICE_NAME_ENUM_SNAPSHOT


def snapshot_tables(promotion_ids: list[int] | None = None, plan_ids: list[int] | None = None,
                    limit: int | None = None) -> dict[str, tuple[str, str, str]]:
    """快照查询的表定义。未指定 id 时选取全部 is_offer_extracted 非 true 的 active 促销与会员；
    指定 id 时只取这些行；limit 为促销与会员合计的最大源数量（促销在前）。"""
    by_id = promotion_ids is not None or plan_ids is not None
    if by_id:
        promo_where = f"is_active and promotion_id = any({_id_array(promotion_ids)})" if promotion_ids else "false"
        plan_where = f"plan_id = any({_id_array(plan_ids)})" if plan_ids else "false"
    else:
        promo_where = "is_active and is_offer_extracted is not true"
        plan_where = "is_offer_extracted is not true"
    if limit is not None:
        promo_pick = (f"select promotion_id from public.clinic_promotions where {promo_where} "
                      f"order by promotion_id limit {int(limit)}")
        promo_where = f"promotion_id in ({promo_pick})"
        plan_where = (f"plan_id in (select plan_id from public.clinic_memberships where {plan_where} "
                      f"order by plan_id limit greatest({int(limit)} - (select count(*) from ({promo_pick}) p), 0))")
    offers_where = (f"promotion_id in (select promotion_id from public.clinic_promotions where {promo_where}) "
                    f"or membership_plan_id in (select plan_id from public.clinic_memberships where {plan_where})")
    items_where = f"offer_id in (select id from public.promo_offer_master where {offers_where})"
    return {
        "promotions": (PROMOTIONS, SOURCE_SPEC[PROMOTIONS]["columns"], promo_where),
        "memberships": (MEMBERSHIPS, SOURCE_SPEC[MEMBERSHIPS]["columns"], plan_where),
        "offers": ("promo_offer_master", OFFER_COLUMNS, offers_where),
        "items": ("promo_offer_items", ITEM_COLUMNS, items_where),
        "services": ("clinic_services", "service_id, business_id",
                     f"service_id in (select service_id from public.promo_offer_items "
                     f"where service_id is not null and {items_where})"),
        "service_enum": SERVICE_NAME_ENUM_SNAPSHOT,
    }


def select_sources(snapshot: Mapping[str, list[dict[str, Any]]],
                   limit: int | None = None) -> list[tuple[str, dict[str, Any]]]:
    """快照中的待抽取源：促销在前、会员在后，各按 id 升序。"""
    selected: list[tuple[str, dict[str, Any]]] = []
    for table, spec in SOURCE_SPEC.items():
        selected += [(table, r) for r in sorted(snapshot[spec["snapshot"]], key=lambda r: r[spec["id"]])]
    return selected[:limit]


def load_existing(snapshot: Mapping[str, list[dict[str, Any]]], table: str, ids: list[int]):
    fk = SOURCE_SPEC[table]["offer_fk"]
    wanted = set(ids)
    offers = [o for o in snapshot["offers"] if o[fk] in wanted]
    offer_ids = {o["id"] for o in offers}
    grouped: dict[int, list[dict[str, Any]]] = {}
    for item in snapshot["items"]:
        if item["offer_id"] in offer_ids:
            grouped.setdefault(item["offer_id"], []).append(item)
    return offers, grouped


def build_plan(snapshot: Mapping[str, list[dict[str, Any]]], client: Any, schema: dict[str, Any], cache_dir: Path,
               limit: int | None = None) -> dict[str, Any]:
    selected = select_sources(snapshot, limit)
    existing = {t: load_existing(snapshot, t, [r[SOURCE_SPEC[t]["id"]] for k, r in selected if k == t]) for t in SOURCE_SPEC}
    service_business = {s["service_id"]: s["business_id"] for s in snapshot["services"]}
    sources = []
    for table, row in selected:
        spec = SOURCE_SPEC[table]
        evidence = source_evidence(table, row)
        payload = extract_source(client, schema, table, row, evidence, cache_dir) if evidence.strip() else None
        offers, items = existing[table]
        fk = spec["offer_fk"]
        own = [o for o in offers if o[fk] == row[spec["id"]]]
        own_items = {o["id"]: items.get(o["id"], []) for o in own}
        valid = {sid for sid, business in service_business.items() if business == row["business_id"]}
        sources.append(plan_source(table, row, payload, evidence, own, own_items, valid))
    return {"generated_at": datetime.now(timezone.utc).isoformat(), "sources": sources}


def dump_plan(plan: dict[str, Any]) -> str:
    return json.dumps(plan, ensure_ascii=False, indent=2, default=lambda v: float(v) if isinstance(v, Decimal) else str(v))
