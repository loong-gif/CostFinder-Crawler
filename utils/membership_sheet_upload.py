"""把上传表 clinic_memberships 工作表中 QA 通过、权益完整的会员行写入数据库。

筛选条件：qa_status 为 PASS；会员名称非空；价格为正数；权益是非空字符串组成的列表且不含链接残留；
同一（商家、页面身份键、会员名称）只保留一行，且数据库中尚无该行。
写入按唯一索引 uq_clinic_memberships_business_source_name 的键，已存在的行不覆盖。
"""
from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any, Mapping

import openpyxl

from utils.pipeline_db import do_block, literal, plan_digest
from utils.source_url import source_url_key

SHEET = "clinic_memberships"
BILLING_PERIODS = frozenset({"monthly", "annual", "quarterly", "one_time", "every_14_days"})
LINK_RESIDUE = re.compile(r"\]\(|https?://|!\[|javascript:", re.I)
NAME_MAX_LENGTH = 120
MIN_BENEFIT_LENGTH = 3

SNAPSHOT_TABLES = {
    "memberships": ("clinic_memberships", "business_id, source_url, membership_name", "true"),
    "businesses": ("master_business_info", "business_id, name", "true"),
}


def business_key(name: Any) -> str:
    return re.sub(r"[^a-z0-9]", "", str(name or "").lower())


def read_sheet_business_names(path: Path) -> dict[int, str]:
    """上传表 master_business_info 工作表中 business_id 对应的商家名称。"""
    rows = list(openpyxl.load_workbook(path, read_only=True)["master_business_info"].iter_rows(values_only=True))
    header = list(rows[0])
    return {int(r[header.index("business_id")]): r[header.index("name")]
            for r in rows[1:] if r[header.index("business_id")] is not None}


def identity(business_id: Any, source_url: Any, name: Any) -> tuple[int, str, str]:
    return int(business_id), source_url_key(source_url), str(name or "").strip().lower()


def read_sheet_rows(path: Path) -> list[dict[str, Any]]:
    rows = list(openpyxl.load_workbook(path, read_only=True)[SHEET].iter_rows(values_only=True))
    header = list(rows[0])
    result = []
    for position, values in enumerate(rows[1:], start=2):
        if any(value is not None for value in values):
            result.append({**dict(zip(header, values)), "sheet_row": position})
    return result


def _number(value: Any) -> float | None:
    if value is None or str(value).strip() == "":
        return None
    return float(str(value).replace("$", "").replace(",", "").strip())


def screen_row(row: Mapping[str, Any], businesses: set[int]) -> tuple[dict[str, Any] | None, str]:
    """返回 (可写入的行, "")；不可写入时返回 (None, 原因)。"""
    if row.get("qa_status") != "PASS":
        return None, f"qa_status_{row.get('qa_status')}"
    if row.get("business_id") is None or int(row["business_id"]) not in businesses:
        return None, "business_missing_or_name_differs_in_database"
    name = str(row.get("membership_name") or "").strip()
    if not name:
        return None, "empty_name"
    if len(name) > NAME_MAX_LENGTH:
        return None, "name_too_long"
    price = _number(row.get("membership_price"))
    if price is None or price <= 0:
        return None, "price_missing_or_not_positive"
    benefits = json.loads(row["benefits"]) if isinstance(row.get("benefits"), str) else row.get("benefits")
    if not isinstance(benefits, list) or not benefits:
        return None, "benefits_empty"
    if any(not isinstance(b, str) or len(b.strip()) < MIN_BENEFIT_LENGTH for b in benefits):
        return None, "benefit_line_empty_or_too_short"
    if any(LINK_RESIDUE.search(b) for b in benefits):
        return None, "benefit_link_residue"
    period = row.get("billing_period")
    if period is not None and period not in BILLING_PERIODS:
        return None, "billing_period_unknown"
    months = _number(row.get("minimum_commitment_months"))
    if months is not None and (months < 0 or months != int(months)):
        return None, "commitment_months_not_whole_number"
    months = None if months is None else int(months)
    source_url = str(row.get("source_url") or "").strip()
    if not source_url:
        return None, "source_url_missing"
    return {"sheet_row": row["sheet_row"], "business_id": int(row["business_id"]), "source_url": source_url,
            "membership_name": name, "membership_price": price, "billing_period": period,
            "minimum_commitment_months": months, "benefits": [b.strip() for b in benefits]}, ""


def build_plan(rows: list[Mapping[str, Any]], snapshot: Mapping[str, list[Mapping[str, Any]]],
               sheet_business_names: Mapping[int, str]) -> dict[str, Any]:
    """商家 ID 必须在库里，且库里的商家名称与上传表 master_business_info 中的名称一致（ID 可能被后来登记的商家占用）。"""
    database_names = {int(r["business_id"]): r["name"] for r in snapshot["businesses"]}
    businesses = {b for b, name in database_names.items()
                  if b in sheet_business_names and business_key(name) == business_key(sheet_business_names[b])}
    existing = {identity(r["business_id"], r["source_url"], r["membership_name"]) for r in snapshot["memberships"]}
    entries: list[dict[str, Any]] = []
    rejected: list[dict[str, Any]] = []
    seen: set[tuple[int, str, str]] = set()
    for row in rows:
        entry, reason = screen_row(row, businesses)
        if entry is None:
            rejected.append({"sheet_row": row["sheet_row"], "membership_name": row.get("membership_name"),
                             "source_url": row.get("source_url"), "reason": reason})
            continue
        key = identity(entry["business_id"], entry["source_url"], entry["membership_name"])
        if key in existing:
            continue
        if key in seen:
            rejected.append({"sheet_row": row["sheet_row"], "membership_name": entry["membership_name"],
                             "source_url": entry["source_url"], "reason": "duplicate_in_sheet"})
        else:
            seen.add(key)
            entries.append(entry)
    return {"entries": entries, "rejected": rejected}


def render_apply(plan: Mapping[str, Any]) -> str:
    entries = plan["entries"]
    statements = []
    for e in entries:
        statements.append(
            "insert into public.clinic_memberships (business_id, source_url, membership_name, membership_price, "
            "billing_period, minimum_commitment_months, benefits, created_at, updated_at) values ("
            f"{literal(e['business_id'])}, {literal(e['source_url'])}, {literal(e['membership_name'])}, "
            f"{literal(e['membership_price'])}, {literal(e['billing_period'])}, "
            f"{literal(e['minimum_commitment_months'])}, {literal(json.dumps(e['benefits'], ensure_ascii=False))}::jsonb, "
            "now(), now()) on conflict (business_id, public.fn_source_url_key(source_url), "
            "lower(btrim(membership_name))) do nothing;")
        statements.append("get diagnostics v_rows = row_count; v_inserted := v_inserted + v_rows;")
    statements.append(
        f"if v_inserted <> {len(entries)} then raise exception 'stale_plan: % of {len(entries)} memberships were "
        "inserted', v_inserted; end if;")
    return do_block(statements, declare=["v_rows integer", "v_inserted integer := 0"],
                    header=f"upload-sheet-memberships apply\nplan sha256 {plan_digest(plan)}\nrows {len(entries)}")
