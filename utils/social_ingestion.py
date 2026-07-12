"""Shared, side-effect-free helpers for social-media ingestion CLIs."""

from __future__ import annotations

import json
import subprocess
from datetime import date, datetime, time, timedelta, timezone
from pathlib import Path
from typing import Any, Dict, Iterator, List, Optional, Sequence, Tuple, TYPE_CHECKING
from zoneinfo import ZoneInfo

if TYPE_CHECKING:
    from utils.supabase_rest import SupabaseRestClient


def fetch_all_rows(
    client: "SupabaseRestClient",
    table: str,
    select: str,
    *,
    filters: Optional[Dict[str, str]] = None,
    page_size: int = 500,
    order: Optional[str] = None,
) -> List[Dict[str, Any]]:
    """Fetch a REST table in pages without coupling callers to pagination."""
    rows: List[Dict[str, Any]] = []
    offset = 0
    while True:
        batch = client.fetch_rows(
            table,
            select,
            filters=filters,
            limit=page_size,
            offset=offset,
            order=order,
        )
        if not batch:
            break
        rows.extend(batch)
        if len(batch) < page_size:
            break
        offset += page_size
    return rows


def resolve_target_date(local_date_arg: Optional[str], timezone_name: str) -> date:
    """Resolve an explicit local date or today's date in the configured zone."""
    if local_date_arg:
        return date.fromisoformat(local_date_arg)
    return datetime.now(ZoneInfo(timezone_name)).date()


def write_json(path: Path, payload: Dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")


def run_cli_json(command: Sequence[str]) -> Dict[str, Any]:
    """Run an external CLI and require a JSON object on stdout."""
    result = subprocess.run(command, capture_output=True, text=True, check=False)
    if result.returncode != 0:
        raise RuntimeError((result.stderr or result.stdout or "").strip() or "命令执行失败")
    try:
        payload = json.loads(result.stdout)
    except json.JSONDecodeError as exc:
        raise RuntimeError(f"命令输出不是合法 JSON: {exc}") from exc
    if not isinstance(payload, dict):
        raise RuntimeError("命令输出格式异常，预期为 JSON 对象")
    return payload


def fetch_dataset_items(dataset_id: str) -> List[Dict[str, Any]]:
    """Read Apify dataset items as a validated list of JSON objects."""
    result = subprocess.run(
        ["apify", "datasets", "get-items", dataset_id, "--format", "json"],
        capture_output=True,
        text=True,
        check=False,
    )
    if result.returncode != 0:
        raise RuntimeError((result.stderr or result.stdout or "").strip() or "拉取 dataset 失败")
    try:
        payload = json.loads(result.stdout)
    except json.JSONDecodeError as exc:
        raise RuntimeError(f"dataset 输出不是合法 JSON: {exc}") from exc
    if not isinstance(payload, list):
        raise RuntimeError("dataset 输出格式异常，预期为 JSON 数组")
    return [item for item in payload if isinstance(item, dict)]


def chunked(items: Sequence[str], size: int) -> Iterator[List[str]]:
    chunk_size = max(1, size)
    for start in range(0, len(items), chunk_size):
        yield list(items[start : start + chunk_size])


def local_day_bounds_utc(target_date: date, timezone_name: str) -> Tuple[datetime, datetime]:
    zone = ZoneInfo(timezone_name)
    local_start = datetime.combine(target_date, time.min, tzinfo=zone)
    local_end = local_start + timedelta(days=1)
    return local_start.astimezone(timezone.utc), local_end.astimezone(timezone.utc)


def local_date_window_bounds_utc(
    start_date: date,
    end_date: date,
    timezone_name: str,
) -> Tuple[datetime, datetime]:
    zone = ZoneInfo(timezone_name)
    local_start = datetime.combine(start_date, time.min, tzinfo=zone)
    local_end = datetime.combine(end_date + timedelta(days=1), time.min, tzinfo=zone)
    return local_start.astimezone(timezone.utc), local_end.astimezone(timezone.utc)


def stringify_timestamp(value: Any) -> str:
    if value is None:
        return ""
    if isinstance(value, str):
        return value.strip()
    return str(value)
