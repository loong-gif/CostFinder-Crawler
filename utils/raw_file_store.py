"""Local JSONL store for raw Firecrawl responses.

The production database has no ``firecrawl_scrape_raw`` / ``firecrawl_search_raw`` tables, so
``SupabaseRestClient`` routes those two tables here. The interface mirrors the REST client
(PostgREST-style filters, order, limit, offset) so callers do not change.
"""
from __future__ import annotations

import fcntl
import json
import os
import re
import tempfile
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, Iterator, List, Optional

RAW_FILE_TABLES = frozenset({"firecrawl_scrape_raw", "firecrawl_search_raw"})
DEFAULT_RAW_DIR = Path(__file__).resolve().parents[1] / "state" / "raw"

_OPS = ("eq", "neq", "gt", "gte", "lt", "lte", "in", "is", "like", "ilike")


def raw_dir() -> Path:
    override = (os.getenv("COSTFINDER_RAW_DIR") or "").strip()
    return Path(override) if override else DEFAULT_RAW_DIR


def _split_top_level(text: str) -> List[str]:
    parts: List[str] = []
    depth = 0
    current = ""
    for char in text:
        if char == "(":
            depth += 1
        elif char == ")":
            depth -= 1
        if char == "," and depth == 0:
            parts.append(current)
            current = ""
        else:
            current += char
    if current:
        parts.append(current)
    return parts


def _coerce(value: Any, literal: str) -> Any:
    if isinstance(value, bool):
        return literal.lower() == "true"
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        try:
            return type(value)(literal)
        except ValueError:
            return literal
    return literal


def _match_op(value: Any, op: str, literal: str) -> bool:
    if op == "is":
        want = literal.lower()
        if want == "null":
            return value is None
        if want in ("true", "false"):
            return value is (want == "true")
        raise ValueError(f"unsupported is.{literal}")
    if op == "in":
        inner = literal.strip()
        if inner.startswith("(") and inner.endswith(")"):
            inner = inner[1:-1]
        options = [item.strip().strip('"') for item in _split_top_level(inner)]
        return str(value) in options if value is not None else False
    if op in ("like", "ilike"):
        pattern = "^" + re.escape(literal).replace(r"\*", ".*").replace("%", ".*") + "$"
        flags = re.IGNORECASE if op == "ilike" else 0
        return value is not None and re.match(pattern, str(value), flags) is not None
    if value is None:
        return op == "neq"
    target = _coerce(value, literal)
    if isinstance(value, str):
        target = literal
    if op == "eq":
        return value == target
    if op == "neq":
        return value != target
    left, right = (str(value), str(target)) if isinstance(value, str) else (value, target)
    return {"gt": left > right, "gte": left >= right, "lt": left < right, "lte": left <= right}[op]


def _match_condition(row: Dict[str, Any], column: str, expression: str) -> bool:
    negate = False
    if expression.startswith("not."):
        negate = True
        expression = expression[4:]
    op, _, literal = expression.partition(".")
    if op not in _OPS:
        raise ValueError(f"unsupported filter operator: {expression!r}")
    result = _match_op(row.get(column), op, literal)
    return not result if negate else result


def _match_filters(row: Dict[str, Any], filters: Optional[Dict[str, str]]) -> bool:
    for column, expression in (filters or {}).items():
        if column in ("select", "order", "limit", "offset"):
            continue
        if column in ("and", "or"):
            inner = expression.strip()
            if inner.startswith("(") and inner.endswith(")"):
                inner = inner[1:-1]
            checks = []
            for part in _split_top_level(inner):
                col, _, expr = part.partition(".")
                checks.append(_match_condition(row, col, expr))
            if not (all(checks) if column == "and" else any(checks)):
                return False
            continue
        if not _match_condition(row, column, str(expression)):
            return False
    return True


def _sort_rows(rows: List[Dict[str, Any]], order: Optional[str]) -> List[Dict[str, Any]]:
    if not order:
        return rows
    for spec in reversed([item.strip() for item in order.split(",") if item.strip()]):
        column, _, direction = spec.partition(".")
        reverse = direction.startswith("desc")
        nulls_last = "nullsfirst" not in direction
        present = [row for row in rows if row.get(column) is not None]
        missing = [row for row in rows if row.get(column) is None]
        present.sort(key=lambda row: row[column], reverse=reverse)
        rows = present + missing if nulls_last else missing + present
    return rows


def _project(row: Dict[str, Any], select: str) -> Dict[str, Any]:
    if not select or select.strip() == "*":
        return dict(row)
    columns = [item.strip() for item in select.split(",") if item.strip()]
    return {column: row.get(column) for column in columns}


class RawFileStore:
    def __init__(self, directory: Optional[Path] = None) -> None:
        self.directory = Path(directory) if directory else raw_dir()

    def _path(self, table: str) -> Path:
        if table not in RAW_FILE_TABLES:
            raise ValueError(f"not a raw file table: {table}")
        return self.directory / f"{table}.jsonl"

    @contextmanager
    def _locked(self, table: str) -> Iterator[None]:
        self.directory.mkdir(parents=True, exist_ok=True)
        with open(self.directory / f"{table}.lock", "a+") as handle:
            fcntl.flock(handle, fcntl.LOCK_EX)
            try:
                yield
            finally:
                fcntl.flock(handle, fcntl.LOCK_UN)

    def _load(self, table: str) -> List[Dict[str, Any]]:
        path = self._path(table)
        if not path.exists():
            return []
        rows: List[Dict[str, Any]] = []
        with path.open(encoding="utf-8") as handle:
            for line in handle:
                line = line.strip()
                if line:
                    rows.append(json.loads(line))
        return rows

    def _write_all(self, table: str, rows: List[Dict[str, Any]]) -> None:
        path = self._path(table)
        fd, tmp_name = tempfile.mkstemp(dir=self.directory, prefix=f".{table}.", suffix=".tmp")
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as handle:
                for row in rows:
                    handle.write(json.dumps(row, ensure_ascii=False, separators=(",", ":")) + "\n")
            os.replace(tmp_name, path)
        except BaseException:
            Path(tmp_name).unlink(missing_ok=True)
            raise

    def fetch_rows(
        self,
        table: str,
        select: str = "*",
        *,
        filters: Optional[Dict[str, str]] = None,
        limit: Optional[int] = None,
        offset: Optional[int] = None,
        order: Optional[str] = None,
    ) -> List[Dict[str, Any]]:
        with self._locked(table):
            rows = [row for row in self._load(table) if _match_filters(row, filters)]
        rows = _sort_rows(rows, order)
        start = offset or 0
        rows = rows[start:] if limit is None else rows[start:start + limit]
        return [_project(row, select) for row in rows]

    def insert_rows(self, table: str, rows: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
        now = datetime.now(timezone.utc).isoformat()
        with self._locked(table):
            existing = self._load(table)
            next_id = max((int(row.get("id") or 0) for row in existing), default=0) + 1
            created: List[Dict[str, Any]] = []
            for payload in rows:
                row = dict(payload)
                row.setdefault("id", next_id)
                row.setdefault("created_at", now)
                row.setdefault("updated_at", now)
                next_id = max(next_id, int(row["id"])) + 1
                created.append(row)
            self._write_all(table, existing + created)
        return [dict(row) for row in created]

    def update_row(self, table: str, row_id_or_filters: Any, payload: Dict[str, Any]) -> List[Dict[str, Any]]:
        filters = (
            dict(row_id_or_filters)
            if isinstance(row_id_or_filters, dict)
            else {"id": f"eq.{row_id_or_filters}"}
        )
        with self._locked(table):
            rows = self._load(table)
            updated: List[Dict[str, Any]] = []
            for row in rows:
                if _match_filters(row, filters):
                    row.update(payload)
                    updated.append(dict(row))
            if updated:
                self._write_all(table, rows)
        return updated

    def delete_rows(self, table: str, filters: Dict[str, str]) -> List[Dict[str, Any]]:
        if not filters:
            raise ValueError("delete_rows requires filters")
        with self._locked(table):
            rows = self._load(table)
            kept = [row for row in rows if not _match_filters(row, filters)]
            removed = [row for row in rows if _match_filters(row, filters)]
            if removed:
                self._write_all(table, kept)
        return removed

    def upsert_rows(
        self, table: str, rows: List[Dict[str, Any]], *, on_conflict: Optional[str] = None
    ) -> List[Dict[str, Any]]:
        key = on_conflict or "id"
        result: List[Dict[str, Any]] = []
        for payload in rows:
            value = payload.get(key)
            matched = (
                self.update_row(table, {key: f"eq.{value}"}, payload) if value is not None else []
            )
            result.extend(matched or self.insert_rows(table, [payload]))
        return result
