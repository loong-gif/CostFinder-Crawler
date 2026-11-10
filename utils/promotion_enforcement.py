"""按入库规则复查存量 active 促销：违反 active 规则（过期、价目页无折扣证据、hub 子页面、空标题）的行改为 inactive。

数据库触发器只拦截新的写入；本模块处理触发器上线前已存在的违规行。下游 offer 由 trg_cascade_promotion_active_to_offers 一并停用。
"""
from __future__ import annotations

from datetime import datetime, timezone
from typing import Any, Mapping

from utils.pipeline_db import do_block, expect_updated_at, literal, plan_digest
from utils.promotion_rules import PromotionRejected, check_promotion

SNAPSHOT_TABLES = {
    "promotions": ("clinic_promotions",
                   "promotion_id, business_id, source_url, promotion_title, promotion_content, campaign_end_date, "
                   "is_active, updated_at",
                   "is_active"),
    "offers": ("promo_offer_master", "id, promotion_id, is_active", "is_active and promotion_id is not null"),
}


def build_plan(snapshot: Mapping[str, list[dict[str, Any]]]) -> dict[str, Any]:
    active_offers: dict[int, int] = {}
    for offer in snapshot["offers"]:
        active_offers[offer["promotion_id"]] = active_offers.get(offer["promotion_id"], 0) + 1
    violations = []
    for row in sorted(snapshot["promotions"], key=lambda r: r["promotion_id"]):
        content = [str(line) for line in row["promotion_content"]] if isinstance(row["promotion_content"], list) else []
        try:
            check_promotion(row["source_url"], row["promotion_title"] or "", content, is_active=True,
                            campaign_end_date=row["campaign_end_date"])
            continue
        except PromotionRejected as exc:
            rule = exc.code
        violations.append({
            "promotion_id": row["promotion_id"], "business_id": row["business_id"],
            "source_url": row["source_url"], "promotion_title": row["promotion_title"],
            "rule": rule, "content": content, "active_offers": active_offers.get(row["promotion_id"], 0),
            "updated_at": row["updated_at"].isoformat(),
        })
    return {"generated_at": datetime.now(timezone.utc).isoformat(),
            "active_promotions": len(snapshot["promotions"]), "violations": violations}


def deactivate_statements(promotion_id: int, updated_at: str) -> list[str]:
    """核对 updated_at 后把一条 active 促销改为 inactive；需要声明 v_count bigint。"""
    label = f"promotion {promotion_id}"
    return [expect_updated_at("clinic_promotions", "promotion_id", promotion_id, updated_at, label),
            f"update public.clinic_promotions set is_active = false, updated_at = now() "
            f"where promotion_id = {literal(promotion_id)} and is_active;",
            "get diagnostics v_count = row_count;",
            f"if v_count <> 1 then raise exception {literal('stale_plan: ' + label + ' not active')}; end if;"]


def render_apply(plan: Mapping[str, Any]) -> str:
    """违规行逐条核对 updated_at 后改为 inactive；任一行已变化即整体回滚。"""
    statements = [s for row in plan["violations"] for s in deactivate_statements(row["promotion_id"], row["updated_at"])]
    header = (f"enforce-promotion-rules apply\nplan sha256 {plan_digest(plan)}\n"
              f"deactivate {len(plan['violations'])} promotions")
    return do_block(statements, declare=["v_count bigint"], header=header)
