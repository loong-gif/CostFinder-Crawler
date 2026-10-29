"""列出 pending_sql/ 中待执行的 SQL 文件及其头部摘要；--mark-applied 把已经执行的文件移入 applied/。

监控主机上的无人值守任务只生成 SQL 文件，不写库；会话中把这些文件拉回本地，审阅后经 Supabase MCP 执行，
执行完成后用 --mark-applied 登记。
"""
from __future__ import annotations

import argparse
from pathlib import Path

from utils.pending_sql import list_pending, mark_applied
from utils.pipeline_run import PROJECT_ROOT

DEFAULT_DIRS = (
    PROJECT_ROOT / "output" / "self_hosted_monitor_state" / "pending_sql",
    PROJECT_ROOT / "output" / "data_quality_state" / "pending_sql",
)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--dir", type=Path, action="append",
                        help="pending_sql directory (repeatable); default: the local monitor and data-quality state directories")
    parser.add_argument("--mark-applied", type=Path, metavar="FILE",
                        help="move an executed SQL file into the applied/ directory next to it")
    args = parser.parse_args()
    if args.mark_applied:
        print(mark_applied(args.mark_applied))
        return 0
    for directory in args.dir or DEFAULT_DIRS:
        entries = list_pending(directory) if directory.is_dir() else []
        print(f"{directory}: {len(entries)} pending")
        for path, header in entries:
            print(f"  {path}")
            for line in header:
                print(f"    {line}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
