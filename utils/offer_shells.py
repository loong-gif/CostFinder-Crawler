"""删除空壳 offer：inactive、没有 item、创建超过 24 小时、且没有任何用户数据引用的 promo_offer_master 行（批次闸门 I）。

被 claims、saved_deals、deal_reports、listing_requests、offer_confirmations 引用的空壳不删除，单列为 blocked。
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Any, Mapping

from utils.pipeline_db import do_block, expect_updated_at, literal, plan_digest

USER_REFERENCES = {
    "claims": "deal_id", "saved_deals": "deal_id", "deal_reports": "offer_id",
    "listing_requests": "offer_id", "offer_confirmations": "offer_id",
}
SHELL_WHERE = ("not is_active and created_at < now() - interval '24 hours' "
               "and not exists (select 1 from public.promo_offer_items i where i.offer_id = promo_offer_master.id)")
SNAPSHOT_TABLES = {
    "shells": ("promo_offer_master", "id, business_id, promotion_id, membership_plan_id, offer_raw_text, created_at, "
                                     "updated_at", SHELL_WHERE),
    **{f"ref_{table}": (table, f"{column} as offer_id",
                        f"{column} in (select id from public.promo_offer_master where {SHELL_WHERE})")
       for table, column in USER_REFERENCES.items()},
}


def build_plan(snapshot: Mapping[str, list[dict[str, Any]]]) -> dict[str, Any]:
    referenced: dict[int, list[str]] = {}
    for table in USER_REFERENCES:
        for row in snapshot[f"ref_{table}"]:
            referenced.setdefault(row["offer_id"], []).append(table)
    cutoff = datetime.now(timezone.utc) - timedelta(hours=24)
    shells, blocked = [], []
    for row in sorted(snapshot["shells"], key=lambda r: r["id"]):
        if row["created_at"] >= cutoff:
            raise RuntimeError(f"snapshot_row_too_recent: offer {row['id']}")
        entry = {"id": row["id"], "business_id": row["business_id"], "promotion_id": row["promotion_id"],
                 "membership_plan_id": row["membership_plan_id"], "offer_raw_text": row["offer_raw_text"],
                 "updated_at": row["updated_at"].isoformat()}
        if row["id"] in referenced:
            blocked.append({**entry, "referenced_by": sorted(set(referenced[row["id"]]))})
        else:
            shells.append(entry)
    return {"generated_at": datetime.now(timezone.utc).isoformat(), "delete": shells, "blocked": blocked}


def render_apply(plan: Mapping[str, Any]) -> str:
    """逐个核对仍是无 item、无用户引用的 inactive 空壳且未变化，再删除；任一不符整体回滚。"""
    statements: list[str] = []
    for shell in plan["delete"]:
        offer_id = literal(shell["id"])
        label = f"offer {shell['id']}"
        statements.append(expect_updated_at("promo_offer_master", "id", shell["id"], shell["updated_at"], label))
        checks = [f"exists (select 1 from public.promo_offer_items where offer_id = {offer_id})",
                  f"(select is_active from public.promo_offer_master where id = {offer_id})"]
        checks += [f"exists (select 1 from public.{table} where {column} = {offer_id})"
                   for table, column in USER_REFERENCES.items()]
        statements.append(f"if {' or '.join(checks)} then raise exception {literal('stale_plan: ' + label + ' is no longer an unreferenced shell')}; end if;")
        statements.append(f"delete from public.promo_offer_master where id = {offer_id};")
        statements.append("get diagnostics v_count = row_count;")
        statements.append(f"if v_count <> 1 then raise exception {literal('stale_plan: ' + label + ' delete count')}; end if;")
    header = (f"delete-offer-shells apply\nplan sha256 {plan_digest(plan)}\n"
              f"delete {len(plan['delete'])} shells, blocked {len(plan['blocked'])}")
    return do_block(statements, declare=["v_count bigint"], header=header)
