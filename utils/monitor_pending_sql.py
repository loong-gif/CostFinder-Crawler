"""monitor 计划到 pending SQL：写入前的策略门（图片证据、证据时效），通过后渲染 DO 块并写入 pending_sql/。"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from pathlib import Path

from utils.monitor_image_gate import image_write_block
from utils.monitor_price_writer import plan_summary, render_apply
from utils.monitor_price_writer_plan import WriterError
from utils.pending_sql import APPLIED_DIR_NAME, pending_dir, write_pending
from utils.pipeline_db import plan_digest

EVIDENCE_MAX_AGE = timedelta(hours=24)


def branch_plans(plan) -> list:
    """连锁分店共用来源页面时计划按分店拆成 branches，每家一份；单家商家的计划本身就是唯一一份。"""
    return plan.get("branches") or [plan]


def plan_write_block(plan, journal_dir):
    """任一分店的计划被写入策略拦下时，整个来源页面都不写入。"""
    return next((block for block in (image_write_block(branch, journal_dir) for branch in branch_plans(plan)) if block),
                None)


def render_pending(plan, protections, state_dir: Path) -> dict:
    """返回 {status: pending_sql, sql_path, ...} 或 {status: review, category}；不连库。

    连锁分店的计划每家渲染一个 DO 块，写进同一个文件；一次执行时在同一个事务里提交。
    """
    block = plan_write_block(plan, pending_dir(state_dir))
    if block:
        return {"status": "review", "category": block}
    if plan.get("prepared_at"):
        prepared = datetime.fromisoformat(plan["prepared_at"].replace("Z", "+00:00"))
        if datetime.now(timezone.utc) - prepared > EVIDENCE_MAX_AGE:
            return {"status": "review", "category": "evidence_expired"}
    try:
        sql_text = "\n".join(render_apply(branch, protections) for branch in branch_plans(plan))
    except WriterError as exc:
        return {"status": "review", "category": str(exc)}
    digest = plan_digest(plan)
    plan_id = str(plan.get("event_id") or digest[:16])
    summary = {**plan_summary(plan), "business_ids": [branch["source"]["business_id"] for branch in branch_plans(plan)]}
    path = write_pending(state_dir, plan_id, digest, summary, sql_text)
    return {"status": "pending_sql", "plan_id": plan_id, "plan_sha256": digest, "sql_path": str(path)}


def settle(receipt: dict, state_dir: Path) -> str:
    """pending_sql 事件的当前状态：文件已移入 applied/ 为 completed，仍在 pending_sql/ 为 pending_sql，其余为 retryable。"""
    path = Path(receipt["sql_path"])
    if (pending_dir(state_dir) / APPLIED_DIR_NAME / path.name).is_file():
        return "completed"
    return "pending_sql" if path.is_file() else "retryable"
