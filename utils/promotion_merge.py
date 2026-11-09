"""按促销身份规则合并 clinic_promotions：同一 (business_id, 规范化 URL) 只留一行，hub 子页面并入 hub。

读取：SNAPSHOT_TABLES 生成的快照；计划冻结为 JSON；应用：render_apply 渲染的 DO 块逐组核对 updated_at 未变化后执行。
"""
from __future__ import annotations

import json
from collections import defaultdict
from datetime import date, datetime, timezone
from typing import Any, Callable, Mapping

from psycopg.types.json import Jsonb

from utils.page_title import TitleUnavailable
from utils.pipeline_db import (
    LOCK_PROMOTIONS, NEXT_PROMOTION_ID, do_block, expect_updated_at, literal, plan_digest, render,
)
from utils.promotion_rules import PromotionRejected, check_promotion
from utils.source_url import hub_url, source_url_key

PROMOTION_COLUMNS = (
    "promotion_id, business_id, promotion_title, source_url, campaign_start_date, campaign_end_date, "
    "is_active, promotion_content, is_ocr_required, screenshot, "
    "requires_cf_verification, updated_at"
)
SNAPSHOT_TABLES = {
    "promotions": ("clinic_promotions", PROMOTION_COLUMNS, "true"),
    "businesses": ("master_business_info", "business_id, name",
                   "business_id in (select business_id from public.clinic_promotions)"),
    "offers": ("promo_offer_master", "id, promotion_id, is_active", "promotion_id is not null"),
}


def _content(row: dict[str, Any]) -> list[str]:
    value = row.get("promotion_content")
    return [str(line) for line in value] if isinstance(value, list) else []


def _keeper_rank(row: dict[str, Any], offers: dict[int, dict[str, int]]) -> tuple:
    stats = offers.get(row["promotion_id"], {})
    return (bool(row["is_active"]), stats.get("active", 0), stats.get("total", 0),
            row["updated_at"], -row["promotion_id"])


def _merged_dates(rows: list[dict[str, Any]]) -> tuple[date | None, date | None]:
    starts = [r["campaign_start_date"] for r in rows]
    ends = [r["campaign_end_date"] for r in rows]
    start = None if not starts or None in starts else min(starts)
    end = None if not ends or None in ends else max(ends)
    return start, end


def build_plan(snapshot: Mapping[str, list[dict[str, Any]]], fetch_title: Callable[[str, str], str]) -> dict[str, Any]:
    promotions = snapshot["promotions"]
    names = {r["business_id"]: r["name"] for r in snapshot["businesses"]}
    offers: dict[int, dict[str, int]] = defaultdict(lambda: {"total": 0, "active": 0})
    offer_ids: dict[int, list[int]] = defaultdict(list)
    for o in snapshot["offers"]:
        offers[o["promotion_id"]]["total"] += 1
        offers[o["promotion_id"]]["active"] += int(bool(o["is_active"]))
        offer_ids[o["promotion_id"]].append(o["id"])

    groups: dict[tuple[int, str], list[dict[str, Any]]] = defaultdict(list)
    for row in promotions:
        target = hub_url(row["source_url"]) or row["source_url"]
        groups[(row["business_id"], source_url_key(target))].append(row)

    planned = []
    for (business_id, key), rows in sorted(groups.items()):
        exact = [r for r in rows if source_url_key(r["source_url"]) == key]
        if len(rows) == 1 and exact:
            continue
        keeper = max(exact, key=lambda r: _keeper_rank(r, offers)) if exact else None
        target_url = keeper["source_url"] if keeper else hub_url(rows[0]["source_url"])
        active_rows = [r for r in rows if r["is_active"]]
        content_rows = ([keeper] if keeper else []) + [r for r in active_rows if r is not keeper]
        content: list[str] = []
        for r in content_rows:
            content.extend(line for line in _content(r) if line not in content)
        if keeper and keeper["is_active"]:
            start, end = keeper["campaign_start_date"], keeper["campaign_end_date"]
        else:
            start, end = _merged_dates(active_rows or rows)
        losers = [r for r in rows if r is not keeper]
        try:
            title, title_error = fetch_title(target_url, names.get(business_id, "")), None
        except TitleUnavailable as exc:
            # 页面本身取不到可用标题是该组的结果；网络与代码错误直接抛出
            title, title_error = None, f"{type(exc).__name__}: {exc}"
            if keeper is not None and not active_rows and keeper["promotion_title"]:
                # 全部成员已 inactive 且页面已不可访问：没有新的页面证据，沿用保留行已有的标题完成合并
                title = keeper["promotion_title"]
        is_active, rule_deactivated = bool(active_rows), None
        if title and is_active:
            try:
                check_promotion(target_url, title, content, is_active=True, campaign_end_date=end)
            except PromotionRejected as exc:
                # 合并后的行违反 active 规则（过期、价目页无折扣证据）时以 inactive 写入
                is_active, rule_deactivated = False, exc.code
        planned.append({
            "business_id": business_id,
            "business_name": names.get(business_id),
            "target_key": key,
            "target_url": target_url,
            "keeper_id": keeper["promotion_id"] if keeper else None,
            "status": "ready" if title else "blocked_title",
            "title_error": title_error,
            "rule_deactivated": rule_deactivated,
            "new_values": {
                "promotion_title": title,
                "is_active": is_active,
                "promotion_content": content,
                "campaign_start_date": start.isoformat() if start else None,
                "campaign_end_date": end.isoformat() if end else None,
                "is_ocr_required": any(bool(r["is_ocr_required"]) for r in (active_rows or rows)),
                "requires_cf_verification": any(bool(r["requires_cf_verification"]) for r in (active_rows or rows)),
                "screenshot": keeper["screenshot"] if keeper else None,
            },
            "members": [{
                "promotion_id": r["promotion_id"], "source_url": r["source_url"],
                "promotion_title": r["promotion_title"], "is_active": r["is_active"],
                "campaign_end_date": r["campaign_end_date"].isoformat() if r["campaign_end_date"] else None,
                "offers_total": offers.get(r["promotion_id"], {}).get("total", 0),
                "offers_active": offers.get(r["promotion_id"], {}).get("active", 0),
                "updated_at": r["updated_at"].isoformat(),
            } for r in rows],
            "loser_ids": [r["promotion_id"] for r in losers],
            "moved_offer_ids": sorted(i for r in losers for i in offer_ids.get(r["promotion_id"], [])),
        })
    return {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "promotion_rows": len(promotions),
        "groups": planned,
    }


_VALUE_COLUMNS = ("promotion_title", "is_active", "promotion_content", "campaign_start_date", "campaign_end_date",
                  "is_ocr_required", "requires_cf_verification")


def _group_statements(group: Mapping[str, Any]) -> list[str]:
    """一组的 plpgsql 语句：核对成员行与待迁移 offer 未变化，写 keeper，迁移 offer，删除其余成员行。"""
    label = f"promotions {[m['promotion_id'] for m in group['members']]}"
    statements = [expect_updated_at("clinic_promotions", "promotion_id", m["promotion_id"], m["updated_at"], label)
                  for m in group["members"]]
    values = {c: group["new_values"][c] for c in _VALUE_COLUMNS}
    values["promotion_content"] = Jsonb(values["promotion_content"])
    if group["keeper_id"] is None:
        statements.append(render(
            "insert into public.clinic_promotions (promotion_id, business_id, source_url, promotion_title, is_active, "
            "promotion_content, campaign_start_date, campaign_end_date, is_ocr_required, "
            f"requires_cf_verification) values ({NEXT_PROMOTION_ID}, {{business_id}}, {{source_url}}, "
            "{promotion_title}, {is_active}, "
            "{promotion_content}, {campaign_start_date}, {campaign_end_date}, "
            "{is_ocr_required}, {requires_cf_verification}) returning promotion_id into v_keeper;",
            business_id=group["business_id"], source_url=group["target_url"], **values))
    else:
        statements.append(f"v_keeper := {literal(group['keeper_id'])};")
        statements.append(render(
            "update public.clinic_promotions set promotion_title={promotion_title}, is_active={is_active}, "
            "promotion_content={promotion_content}, campaign_start_date={campaign_start_date}, "
            "campaign_end_date={campaign_end_date}, is_ocr_required={is_ocr_required}, requires_cf_verification={requires_cf_verification}, "
            "updated_at=now() where promotion_id=v_keeper;", **values))
    losers = literal(group["loser_ids"]) + "::bigint[]"
    expected_offers = literal(group["moved_offer_ids"]) + "::bigint[]"
    statements.append(
        f"if coalesce((select array_agg(id order by id) from public.promo_offer_master where promotion_id = any({losers})), "
        f"'{{}}'::bigint[]) is distinct from {expected_offers} then "
        f"raise exception {literal('stale_plan: offers under ' + label)}; end if;")
    # keeper 为 inactive 时，迁入的 offer 一并停用（offer 不能挂在 inactive 促销下保持 active）
    statements.append(
        f"update public.promo_offer_master set promotion_id=v_keeper, is_active = is_active and "
        f"{literal(group['new_values']['is_active'])}, updated_at=now() where promotion_id = any({losers});")
    statements.append(f"update public.promotion_content_guardrail_queue set promotion_id=v_keeper "
                      f"where promotion_id = any({losers});")
    statements.append(f"delete from public.clinic_promotions where promotion_id = any({losers});")
    statements.append("get diagnostics v_count = row_count;")
    statements.append(f"if v_count <> {len(group['loser_ids'])} then "
                      f"raise exception {literal('stale_plan: delete count for ' + label)}; end if;")
    return statements


def render_apply(plan: Mapping[str, Any]) -> str:
    """全部 ready 组渲染为单个 DO 块；任一组的成员行或 offer 已变化即整体回滚。"""
    ready = [g for g in plan["groups"] if g["status"] == "ready"]
    statements = [LOCK_PROMOTIONS] if any(g["keeper_id"] is None for g in ready) else []
    for group in ready:
        statements.extend(_group_statements(group))
    header = (f"merge-promotions apply\nplan sha256 {plan_digest(plan)}\n"
              f"groups {len(ready)} of {len(plan['groups'])}")
    return do_block(statements, declare=["v_keeper bigint", "v_count bigint"], header=header)


def verify_applied(plan: Mapping[str, Any], snapshot: Mapping[str, list[dict[str, Any]]]) -> list[str]:
    """执行后核对：每个 ready 组的目标行取值与计划一致、其余成员行已删除、迁移的 offer 挂在目标行下。返回不一致项。"""
    by_key = defaultdict(list)
    for row in snapshot["promotions"]:
        by_key[(row["business_id"], source_url_key(row["source_url"]))].append(row)
    present = {row["promotion_id"] for row in snapshot["promotions"]}
    offer_owner = {o["id"]: o["promotion_id"] for o in snapshot["offers"]}
    problems: list[str] = []
    for group in plan["groups"]:
        if group["status"] != "ready":
            continue
        rows = by_key[(group["business_id"], group["target_key"])]
        if len(rows) != 1:
            problems.append(f"{group['target_url']}: expected one row, found {len(rows)}")
            continue
        row, values = rows[0], group["new_values"]
        actual = {"promotion_title": row["promotion_title"], "is_active": row["is_active"],
                  "promotion_content": row["promotion_content"],
                  "campaign_start_date": row["campaign_start_date"].isoformat() if row["campaign_start_date"] else None,
                  "campaign_end_date": row["campaign_end_date"].isoformat() if row["campaign_end_date"] else None}
        for column, value in actual.items():
            if value != values[column]:
                problems.append(f"{group['target_url']}: {column} is {value!r}, planned {values[column]!r}")
        if group["keeper_id"] is not None and row["promotion_id"] != group["keeper_id"]:
            problems.append(f"{group['target_url']}: keeper {group['keeper_id']} replaced by {row['promotion_id']}")
        problems += [f"{group['target_url']}: member {i} still present" for i in group["loser_ids"] if i in present]
        problems += [f"{group['target_url']}: offer {i} under {offer_owner.get(i)}" for i in group["moved_offer_ids"]
                     if offer_owner.get(i) != row["promotion_id"]]
    return problems


def dump_plan(plan: dict[str, Any]) -> str:
    return json.dumps(plan, ensure_ascii=False, indent=2, default=str)
