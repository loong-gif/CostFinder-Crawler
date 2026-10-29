"""商家登记：把发现结果中的 insert / promote / link / refresh 渲染为单个 DO 块（apply.sql），先在 SQL 中重做去重与匹配核对。"""
from __future__ import annotations

from typing import Any, Iterable, Mapping

from utils.pipeline_db import do_block, literal, plan_digest
from utils.sql_checks import raise_if

MASTER_COLUMNS = ("name", "address", "city", "review_count", "score", "category", "website", "google_place_id")
TABLE = "public.master_business_info"


def _assignments(fields: Mapping[str, Any]) -> str:
    unknown = set(fields) - set(MASTER_COLUMNS)
    if unknown:
        raise ValueError(f"unknown master_business_info columns: {sorted(unknown)}")
    return ", ".join(f"{column} = {literal(value)}" for column, value in fields.items())


def _one_row(label: str) -> str:
    return ("get diagnostics v_count = row_count; "
            f"if v_count <> 1 then raise exception {literal(label)}; end if;")


def render_register(actions: Mapping[str, Iterable[Mapping[str, Any]]], wanted: set[str]) -> str:
    """wanted 中的动作渲染为 SQL：insert/promote 新建商家，link 回填 google_place_id，refresh 刷新指标。"""
    statements: list[str] = []
    counts = {name: 0 for name in ("insert", "promote", "link", "refresh")}
    for name in ("insert", "promote"):
        if name not in wanted:
            continue
        for item in actions[name]:
            row = {column: item.get(column) for column in MASTER_COLUMNS if item.get(column) is not None}
            row["google_place_id"] = item["google_place_id"]
            place = literal(row["google_place_id"])
            statements.append(raise_if(f"exists (select 1 from {TABLE} where google_place_id = {place})",
                                       f"already_registered: {row['google_place_id']}"))
            statements.append(f"insert into {TABLE} ({', '.join(row)}) values ({', '.join(literal(v) for v in row.values())});")
            statements.append(_one_row(f"insert_failed: {row['google_place_id']}"))
            counts[name] += 1
    if "link" in wanted:
        for item in actions["link"]:
            place = literal(item["google_place_id"])
            statements.append(raise_if(f"exists (select 1 from {TABLE} where google_place_id = {place})",
                                       f"place_already_linked: {item['google_place_id']}"))
            fields = {"google_place_id": item["google_place_id"], **item["fields"]}
            statements.append(f"update {TABLE} set {_assignments(fields)} "
                              f"where business_id = {int(item['business_id'])} and google_place_id is null;")
            statements.append(_one_row(f"link_conflict: {int(item['business_id'])}"))
            counts["link"] += 1
    if "refresh" in wanted:
        for item in actions["refresh"]:
            statements.append(f"update {TABLE} set {_assignments(item['fields'])} "
                              f"where business_id = {int(item['business_id'])};")
            statements.append(_one_row(f"refresh_missing: {int(item['business_id'])}"))
            counts["refresh"] += 1
    digest = plan_digest({name: list(actions[name]) for name in counts})
    header = f"register businesses apply\nplan sha256 {digest}\nactions {counts}"
    return do_block(statements, declare=["v_count bigint"], header=header)
