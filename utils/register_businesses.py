"""商家登记：把发现结果中的 insert / promote / link / refresh 渲染为单个 DO 块（apply.sql）。

动作种类由 classify_places 按过滤线判定，全部渲染；每条语句在 SQL 内按 google_place_id 判重，
同一批地点重复渲染、重复执行都不会重复登记或挂接。
"""
from __future__ import annotations

from typing import Any, Iterable, Mapping

from utils.pipeline_db import do_block, literal, plan_digest

MASTER_COLUMNS = ("name", "address", "city", "review_count", "score", "category", "website", "google_place_id")
TABLE = "public.master_business_info"
ACTIONS = ("insert", "promote", "link", "refresh")


def _assignments(fields: Mapping[str, Any]) -> str:
    unknown = set(fields) - set(MASTER_COLUMNS)
    if unknown:
        raise ValueError(f"unknown master_business_info columns: {sorted(unknown)}")
    return ", ".join(f"{column} = {literal(value)}" for column, value in fields.items())


def _not_registered(place_id: str) -> str:
    return f"not exists (select 1 from {TABLE} where google_place_id = {literal(place_id)})"


def render_register(actions: Mapping[str, Iterable[Mapping[str, Any]]]) -> str:
    """insert/promote 新建商家，link 回填 google_place_id，refresh 刷新指标；已登记的地点跳过。"""
    statements: list[str] = []
    counts = {name: 0 for name in ACTIONS}
    seen: set[str] = set()
    for name in ("insert", "promote"):
        for item in actions[name]:
            row = {column: item.get(column) for column in MASTER_COLUMNS if item.get(column) is not None}
            row["google_place_id"] = item["google_place_id"]
            if row["google_place_id"] in seen:
                continue
            seen.add(row["google_place_id"])
            statements.append(f"insert into {TABLE} ({', '.join(row)}) select {', '.join(literal(v) for v in row.values())} "
                              f"where {_not_registered(row['google_place_id'])};")
            counts[name] += 1
    for item in actions["link"]:
        fields = {"google_place_id": item["google_place_id"], **item["fields"]}
        statements.append(f"update {TABLE} set {_assignments(fields)} where business_id = {int(item['business_id'])} "
                          f"and google_place_id is null and {_not_registered(item['google_place_id'])};")
        counts["link"] += 1
    for item in actions["refresh"]:
        statements.append(f"update {TABLE} set {_assignments(item['fields'])} where business_id = {int(item['business_id'])};")
        counts["refresh"] += 1
    digest = plan_digest({name: list(actions[name]) for name in ACTIONS})
    header = f"register businesses apply\nplan sha256 {digest}\nactions {counts}"
    return do_block(statements or ["null;"], header=header)
