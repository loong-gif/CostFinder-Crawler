"""把核验未通过的会员权益置为 NULL（未知）：计划冻结每行当前的 benefits 与 updated_at，渲染为单个 DO 块。

NULL 表示权益未知；页面核验不完整时，未知比错误的权益更安全。
"""
from __future__ import annotations

from typing import Any, Iterable, Mapping

from utils.pipeline_db import do_block, literal, plan_digest, render

COLUMNS = "plan_id, business_id, source_url, membership_name, benefits, is_offer_extracted, updated_at"


def snapshot_tables(plan_ids: Iterable[int]) -> dict[str, tuple[str, str, str]]:
    ids = sorted(set(int(i) for i in plan_ids))
    if not ids:
        raise RuntimeError("no_plan_ids: give at least one --plan-id")
    return {"memberships": ("clinic_memberships", COLUMNS, f"plan_id = any({literal(ids)}::bigint[])")}


def build_plan(snapshot: Mapping[str, list[dict[str, Any]]], reasons: Mapping[int, str]) -> dict[str, Any]:
    rows = {row["plan_id"]: row for row in snapshot["memberships"]}
    missing = sorted(set(reasons) - set(rows))
    if missing:
        raise RuntimeError(f"unknown_plan_ids: {missing}")
    entries = [{"plan_id": plan_id, "membership_name": rows[plan_id]["membership_name"],
                "source_url": rows[plan_id]["source_url"], "reason": reason,
                "benefits_before": rows[plan_id]["benefits"],
                "expected_updated_at": rows[plan_id]["updated_at"]}
               for plan_id, reason in sorted(reasons.items()) if rows[plan_id]["benefits"] is not None]
    return {"entries": entries, "already_null": sorted(p for p in reasons if rows[p]["benefits"] is None)}


def render_apply(plan: Mapping[str, Any]) -> str:
    """每行先锁定并核对 updated_at 与计划一致，再置空 benefits 并重置 is_offer_extracted。"""
    statements = []
    for entry in plan["entries"]:
        label = literal(f"stale_plan: clinic_memberships {entry['plan_id']}")
        statements += [
            render("select updated_at into v_updated from public.clinic_memberships where plan_id = {pid} for update;",
                   pid=entry["plan_id"]),
            render("if not found or v_updated is distinct from {ts}::timestamptz then ",
                   ts=entry["expected_updated_at"]) + f"raise exception {label}; end if;",
            render("update public.clinic_memberships set benefits = null, is_offer_extracted = false, "
                   "updated_at = now() where plan_id = {pid};", pid=entry["plan_id"]),
        ]
    header = f"clear-membership-benefits apply\nplan sha256 {plan_digest(plan)}\nrows {len(plan['entries'])}"
    return do_block(statements, declare=["v_updated timestamptz"], header=header)
