"""Python 代码不直接写生产库：所有写入由流水线命令渲染为 apply.sql，经 Supabase MCP 执行。

REST 写入、psycopg 写入等直接写库路径调用 forbid_direct_write()，一律报错。
"""
from __future__ import annotations

from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
ENTRY_POINT = PROJECT_ROOT / "scripts" / "promo_pipeline.py"


class DirectDatabaseWrite(RuntimeError):
    pass


def forbid_direct_write(action: str) -> None:
    raise DirectDatabaseWrite(
        f"{action}: database writes are rendered to apply.sql by `python scripts/promo_pipeline.py <command>` "
        "and executed through the Supabase MCP")
