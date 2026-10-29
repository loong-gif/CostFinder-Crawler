"""渲染 pending SQL 时共用的 plpgsql 片段：字面量、行核对、版本核对、条件 raise。"""
from __future__ import annotations

from datetime import date, datetime, timezone
from decimal import Decimal
import re
from typing import Any, Iterable, Mapping

from psycopg.types.json import Jsonb

from utils.pipeline_db import literal

JSONB_COLUMNS = frozenset({"promotion_content", "benefits"})
_TIMESTAMP_TEXT = re.compile(r"^\d{4}-\d\d-\d\d[T ]\d\d:\d\d(:\d\d(\.\d+)?)?(Z|[+-]\d\d(:?\d\d)?)?$")


class Raw(str):
    """原样输出的 SQL 片段（变量、表达式）。"""


def wire(value: Any) -> Any:
    """计划值转为可 JSON 序列化的形式（Decimal、日期、时间戳）。"""
    if isinstance(value, dict):
        return {str(k): wire(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [wire(v) for v in value]
    if isinstance(value, Decimal):
        return int(value) if value == value.to_integral_value() else float(value)
    if isinstance(value, datetime):
        return value.astimezone(timezone.utc).isoformat() if value.tzinfo else value.isoformat()
    if isinstance(value, date):
        return value.isoformat()
    return value


def is_timestamp(key: str, value: Any) -> bool:
    return key in {"created_at", "updated_at"} or (isinstance(value, str) and bool(_TIMESTAMP_TEXT.match(value)))


def sql_value(value: Any) -> str:
    return str(value) if isinstance(value, Raw) else literal(value)


def write_value(column: str, value: Any) -> Any:
    """写入用的值：jsonb 列的值用 Jsonb 适配。"""
    if isinstance(value, Raw):
        return value
    if column in JSONB_COLUMNS and value is not None:
        return Jsonb(value)
    return value


def jsonb_literal(value: Any) -> str:
    return f"to_jsonb({value})" if isinstance(value, Raw) else literal(Jsonb(wire(value)))


def raise_if(condition: str, message: str) -> str:
    return f"if {condition} then raise exception {literal(message)}; end if;"


def id_array(values: Iterable[Any]) -> str:
    """整数（或变量）数组的排序表达式，用来与 array_agg(... order by) 的结果比较。"""
    items = ", ".join(str(v) if isinstance(v, Raw) else str(int(v)) for v in values)
    return f"(select coalesce(array_agg(x order by x), '{{}}'::bigint[]) from unnest(array[{items}]::bigint[]) x)"


def _columns_equal(alias: str, row: Mapping[str, Any]) -> list[str]:
    plain = {k: v for k, v in row.items() if not is_timestamp(k, v)}
    stamps = {k: v for k, v in row.items() if is_timestamp(k, v)}
    parts = []
    if plain:
        parts.append(f"(select jsonb_object_agg(e.key, e.value) from jsonb_each(to_jsonb({alias})) e "
                     f"where e.key = any({literal(sorted(plain))}::text[])) = {literal(Jsonb(plain))}")
    for key, value in stamps.items():
        # 时间戳可能为 NULL（例如历史行缺 created_at），用 is not distinct from 才能匹配
        parts.append(f"(to_jsonb({alias}) ->> {literal(key)})::timestamptz is not distinct from "
                     f"{literal(value)}::timestamptz")
    return parts


def row_matches(table: str, pk: str, row: Mapping[str, Any]) -> str:
    """布尔表达式：库中该行的每个给定列都与 row 相同（时间戳按 timestamptz 比较）。"""
    parts = [f"t.{pk} = {int(row[pk])}"] + _columns_equal("t", row)
    return f"exists (select 1 from public.{table} t where {' and '.join(parts)})"


def row_equal_excluding(table: str, pk: str, row: Mapping[str, Any], ignored: Iterable[str]) -> str:
    """同 row_matches，但忽略 ignored 中的列。"""
    skip = set(ignored)
    return row_matches(table, pk, {k: v for k, v in row.items() if k not in skip})


def versions_differ(table: str, pk: str, rows: list[Mapping[str, Any]], scope: str, *, exclude: Iterable[Any] = ()) -> str:
    """布尔表达式：scope（where 子句，别名 t）内的行集合或 updated_at 与 rows 不同。
    rows 无 updated_at 键时只比较行集合；exclude（整数或 Raw 变量）中的行不参与比较。"""
    skipped = sorted(set(exclude), key=str)
    stamped = bool(rows) and all("updated_at" in r for r in rows)
    if rows:
        values = ", ".join(f"({int(r[pk])}, {literal(r['updated_at'])}::timestamptz)" if stamped else f"({int(r[pk])}, null::timestamptz)"
                           for r in rows)
        source = f"(values {values}) v(id, ts)"
    else:
        source = "(select null::bigint, null::timestamptz where false) v(id, ts)"
    guard = (f" and t.{pk} <> all(array[{', '.join(str(v) if isinstance(v, Raw) else str(int(v)) for v in skipped)}]::bigint[])"
             if skipped else "")
    current = (f"(select t.{pk} as id, {'t.updated_at' if stamped else 'null::timestamptz'} as ts "
               f"from public.{table} t where {scope}{guard}) c")
    return (f"exists (select 1 from {source} full join {current} on c.id = v.id "
            f"where v.id is null or c.id is null or v.ts is distinct from c.ts)")
