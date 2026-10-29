"""待执行 SQL 文件：监控主机上的无人值守任务只写 <state_dir>/pending_sql/*.sql，会话中拉回、审阅后经 Supabase MCP 执行。"""
from __future__ import annotations

import json
import re
import shutil
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Mapping

PENDING_DIR_NAME = "pending_sql"
APPLIED_DIR_NAME = "applied"
_ID = re.compile(r"^[A-Za-z0-9_-]+$")


def pending_dir(state_dir: Path) -> Path:
    return Path(state_dir) / PENDING_DIR_NAME


def header_lines(plan_id: str, plan_sha256: str, summary: Mapping[str, Any]) -> str:
    lines = [f"-- plan {plan_id}", f"-- plan sha256 {plan_sha256}"]
    lines.extend(f"-- {key}: {json.dumps(value, ensure_ascii=False, default=str)}" for key, value in summary.items())
    return "\n".join(lines) + "\n"


def write_pending(state_dir: Path, plan_id: str, plan_sha256: str, summary: Mapping[str, Any], sql_text: str) -> Path:
    """写入 <state_dir>/pending_sql/<UTC 时间戳>_<plan_id>.sql，文件头注释含计划摘要与 plan sha256。"""
    if not _ID.match(plan_id):
        raise ValueError(f"invalid plan id: {plan_id!r}")
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S%fZ")
    path = pending_dir(state_dir) / f"{stamp}_{plan_id}.sql"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(header_lines(plan_id, plan_sha256, summary) + sql_text, encoding="utf-8")
    return path


def read_header(path: Path) -> list[str]:
    """文件开头连续的 `-- ` 注释行（去掉前缀）。"""
    header = []
    with Path(path).open(encoding="utf-8") as handle:
        for line in handle:
            if not line.startswith("--"):
                break
            header.append(line[2:].strip())
    return header


def list_pending(directory: Path) -> list[tuple[Path, list[str]]]:
    """pending_sql/ 下待执行的文件：<ts>_<plan id>.sql，以及命令生成的 <ts>/apply.sql。"""
    directory = Path(directory)
    paths = sorted(directory.glob("*.sql")) + sorted(directory.glob("*/apply.sql"))
    return [(path, read_header(path)) for path in paths if path.parent.name != APPLIED_DIR_NAME]


def mark_applied(path: Path) -> Path:
    """把已执行的文件移入 pending_sql/applied/；<ts>/apply.sql 连同其目录一起移动。"""
    path = Path(path)
    if not path.is_file():
        raise FileNotFoundError(path)
    nested = path.name == "apply.sql" and path.parent.parent.name == PENDING_DIR_NAME
    source = path.parent if nested else path
    root = source.parent
    target = root / APPLIED_DIR_NAME / source.name
    target.parent.mkdir(exist_ok=True)
    shutil.move(str(source), target)
    return target / path.name if nested else target
