"""流水线不使用 DB_URL，不建立数据库连接。

读：命令的 plan 步骤经 fresh_snapshot 生成只读快照查询（read.sql），read_snapshot 用 Supabase CLI（--linked）
    执行并直接写入 snapshot.json；加载时逐表核对 count 与行数，结果被截断即报错。
写：命令把计划渲染为单个 DO 块（apply.sql），经 Supabase MCP（execute_sql）执行；块内先核对计划生成后
    数据未变化，再写入，最后执行收尾检查；任何一步失败整体回滚。
"""
from __future__ import annotations

import hashlib
import json
import subprocess
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Any, Iterable, Mapping

import pglast
from psycopg import sql

from utils.pipeline_run import PROJECT_ROOT
from utils.supabase_rest import PRODUCTION_PROJECT_REF

DO_TAG = "$pipeline$"
# clinic_promotions.promotion_id 没有默认序列：插入前锁表，取当前最大值加一
LOCK_PROMOTIONS = "lock table public.clinic_promotions in share row exclusive mode;"
NEXT_PROMOTION_ID = "(select coalesce(max(promotion_id), 0) + 1 from public.clinic_promotions)"
SERVICE_NAME_ENUM_SNAPSHOT = (
    "pg_catalog.pg_enum e join pg_catalog.pg_type t on t.oid = e.enumtypid",
    "e.enumlabel, e.enumsortorder",
    "t.typname = 'clinic_service_name_enum'",
)
DATE_COLUMNS = frozenset({"campaign_start_date", "campaign_end_date"})
TIMESTAMP_COLUMNS = frozenset({"created_at", "updated_at"})


class SnapshotIncomplete(RuntimeError):
    pass


def literal(value: Any) -> str:
    """SQL 字面量，由 psycopg 负责转义。"""
    return sql.Literal(value).as_string(None)


def render(template: str, **params: Any) -> str:
    """模板中的 {name} 替换为对应参数的 SQL 字面量。"""
    return sql.SQL(template).format(**{k: sql.Literal(v) for k, v in params.items()}).as_string(None)


def snapshot_query(tables: Mapping[str, tuple[str, str, str]]) -> str:
    """tables: 名称 → (from 子句, 列, where 条件)；from 子句为不带 schema 的表名时按 public 表处理。
    返回一条只读查询，snapshot 列为 {名称: {count, rows}}。"""
    parts = []
    for name, (source, columns, where) in tables.items():
        source = source if " " in source or "." in source else f"public.{source}"
        parts.append(
            f"{literal(name)}, json_build_object("
            f"'count', (select count(*) from {source} where {where}), "
            f"'rows', coalesce((select json_agg(t) from (select {columns} from {source} where {where}) t), '[]'::json))")
    text = "select json_build_object(\n  " + ",\n  ".join(parts) + "\n) as snapshot;\n"
    pglast.parse_sql(text)
    return text


def read_snapshot(read_sql: Path) -> Path:
    """用 Supabase CLI（--linked，Management API 登录，不用 DB_URL）执行只读快照查询，结果直接写入同目录 snapshot.json。"""
    linked = (PROJECT_ROOT / "supabase" / ".temp" / "project-ref").read_text().strip()
    if linked != PRODUCTION_PROJECT_REF:
        raise RuntimeError(f"database_project_mismatch: supabase CLI is linked to {linked}")
    done = subprocess.run(["supabase", "db", "query", "--linked", "-o", "json", "-f", str(read_sql)],
                          cwd=PROJECT_ROOT, capture_output=True, text=True, check=True)
    rows = json.loads(done.stdout)["rows"]
    if len(rows) != 1 or set(rows[0]) != {"snapshot"}:
        raise RuntimeError(f"unexpected snapshot result shape: {len(rows)} rows")
    return write_text(Path(read_sql).parent / "snapshot.json", json.dumps(rows[0]["snapshot"], ensure_ascii=False))


def run_dir(output_dir: Path) -> Path:
    """命令的一次运行目录，按 UTC 时间命名。"""
    return Path(output_dir) / datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")


def fresh_snapshot(directory: Path, tables: Mapping[str, tuple[str, str, str]]) -> Path:
    """在 directory 写入只读快照查询 read.sql 并立即执行，返回 snapshot.json 的路径。"""
    return read_snapshot(write_text(Path(directory) / "read.sql", snapshot_query(tables)))


def _typed(row: dict[str, Any]) -> dict[str, Any]:
    for column in DATE_COLUMNS & row.keys():
        if row[column] is not None:
            row[column] = date.fromisoformat(row[column])
    for column in TIMESTAMP_COLUMNS & row.keys():
        if row[column] is not None:
            row[column] = datetime.fromisoformat(row[column])
    return row


def load_snapshot(path: Path, expected: Iterable[str], *, typed: bool = True) -> dict[str, list[dict[str, Any]]]:
    """读取 snapshot.json（snapshot 列的值），核对每张表的 count 与行数一致；
    typed 为真时日期与时间戳列转为 date/datetime，否则保留 JSON 原值。"""
    payload = json.loads(Path(path).read_text())
    missing = sorted(set(expected) - set(payload))
    if missing:
        raise SnapshotIncomplete(f"snapshot is missing tables: {missing}")
    result = {}
    for name in expected:
        part = payload[name]
        if part["count"] != len(part["rows"]):
            raise SnapshotIncomplete(f"incomplete_read:{name}: read {len(part['rows'])} of {part['count']}")
        result[name] = [_typed(dict(row)) if typed else dict(row) for row in part["rows"]]
    return result


def do_block(statements: Iterable[str], *, declare: Iterable[str] = (), header: str = "") -> str:
    """把语句包成单个 DO 块；DO 块是一条语句，执行失败即整体回滚。"""
    body = list(statements)
    for statement in body:
        if DO_TAG in statement:
            raise ValueError("statement contains the DO block quote tag")
    lines = [f"-- {line}" for line in header.splitlines()]
    lines.append(f"do {DO_TAG}")
    declared = list(declare)
    if declared:
        lines.append("declare")
        lines.extend(f"  {d};" for d in declared)
    lines.append("begin")
    lines.extend(f"  {s}" for s in body)
    lines.append("end")
    lines.append(f"{DO_TAG};")
    text = "\n".join(lines) + "\n"
    validate_do_block(text)
    return text


def validate_do_block(text: str) -> None:
    """用 PostgreSQL 解析器（pglast）离线校验 DO 块及块内 plpgsql 与嵌入 SQL 的语法，语法错误即抛出 ParseError。"""
    pglast.parse_sql(text)
    body = text.split(f"do {DO_TAG}", 1)[1].rsplit(f"{DO_TAG};", 1)[0]
    pglast.parse_plpgsql(f"create function pipeline_syntax_check() returns void language plpgsql as {DO_TAG}"
                         f"{body}{DO_TAG};")


def expect_updated_at(table: str, key_column: str, key: Any, updated_at: str | None, label: str) -> str:
    """核对一行的 updated_at 与计划一致（None 表示该行必须不存在）。"""
    if updated_at is None:
        condition = render(f"exists (select 1 from public.{table} where {key_column} = {{key}})", key=key)
    else:
        condition = render(
            f"(select updated_at from public.{table} where {key_column} = {{key}}) is distinct from {{ts}}::timestamptz",
            key=key, ts=updated_at)
    return f"if {condition} then raise exception {literal('stale_plan: ' + label)}; end if;"


def plan_digest(plan: Mapping[str, Any]) -> str:
    return hashlib.sha256(json.dumps(plan, sort_keys=True, default=str).encode()).hexdigest()


def write_text(path: Path, text: str) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text)
    return path
