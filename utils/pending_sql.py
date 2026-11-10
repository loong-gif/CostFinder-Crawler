"""待执行 SQL 文件：监控主机上的无人值守任务只写 <state_dir>/pending_sql/*.sql；pending-sql 命令自动拉回，
会话经 Supabase MCP 执行，--mark-applied 同时登记本地与监控主机。"""
from __future__ import annotations

import json
import re
import shutil
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Mapping

PENDING_DIR_NAME = "pending_sql"
APPLIED_DIR_NAME = "applied"
REJECTED_DIR_NAME = "rejected"
_ID = re.compile(r"^[A-Za-z0-9_-]+$")
# 自托管 Firecrawl 串页故障：2026-11-06 15:45 UTC 容器重建，2026-11-07 03:31 UTC 修复（docs/decisions-extraction-stability.md）
FIRECRAWL_OUTAGES = ((datetime(2026, 11, 6, 15, 45, tzinfo=timezone.utc), datetime(2026, 11, 7, 3, 31, tzinfo=timezone.utc)),)


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


def _from_outage(path: Path) -> bool:
    """文件名开头的 UTC 时间戳落在自托管 Firecrawl 串页故障期间：这些计划的抓取内容不可信，不列为待执行。"""
    name = path.parent.name if path.name == "apply.sql" else path.name
    stamp = datetime.strptime(name[:15], "%Y%m%dT%H%M%S").replace(tzinfo=timezone.utc)
    return any(start <= stamp < end for start, end in FIRECRAWL_OUTAGES)


def list_pending(directory: Path) -> list[tuple[Path, list[str]]]:
    """pending_sql/ 下待执行的文件：<ts>_<plan id>.sql，以及命令生成的 <ts>/apply.sql。"""
    directory = Path(directory)
    paths = sorted(directory.glob("*.sql")) + sorted(directory.glob("*/apply.sql"))
    return [(path, read_header(path)) for path in paths
            if path.parent.name != APPLIED_DIR_NAME and not _from_outage(path)]


def _move_out(path: Path, folder: str) -> Path:
    """把文件移入 pending_sql/<folder>/；<ts>/apply.sql 连同其目录一起移动。"""
    path = Path(path)
    if not path.is_file():
        raise FileNotFoundError(path)
    nested = path.name == "apply.sql" and path.parent.parent.name == PENDING_DIR_NAME
    source = path.parent if nested else path
    root = source.parent
    target = root / folder / source.name
    target.parent.mkdir(exist_ok=True)
    shutil.move(str(source), target)
    return target / path.name if nested else target


def mark_applied(path: Path) -> Path:
    """把已执行的文件移入 pending_sql/applied/。"""
    return _move_out(path, APPLIED_DIR_NAME)


def mark_rejected(path: Path, error: str) -> Path:
    """把被自身核对拒绝的文件移入 pending_sql/rejected/，错误原文存为同目录的 <文件>.error.txt。
    监控的 settle 把移出 pending_sql/ 的文件视为缺失，事件回到 retryable 并按新证据重新规划。"""
    target = _move_out(path, REJECTED_DIR_NAME)
    target.with_name(target.name + ".error.txt").write_text(error, encoding="utf-8")
    return target
