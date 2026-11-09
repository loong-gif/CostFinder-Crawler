"""列出待执行的 SQL 文件及其头部摘要；--mark-applied 登记已执行的文件。

监控主机上的无人值守任务只生成 SQL 文件，不写库。列出前自动把监控主机的 pending_sql/ 拉到本地镜像目录；
会话经 Supabase MCP 执行文件后，用 --mark-applied 登记：本地文件移入 applied/，镜像目录中的文件同时在监控主机上
移入 applied/（下次 monitor 运行时对应事件即为 completed）。
"""
from __future__ import annotations

import argparse
from pathlib import Path

from utils.pending_sql import list_pending, mark_applied
from utils.pending_sql_sync import pull_pending, remote_mark_applied
from utils.pipeline_run import PROJECT_ROOT

MONITOR_MIRROR = PROJECT_ROOT / "output" / "self_hosted_monitor_state" / "pending_sql"
DATA_QUALITY_DIR = PROJECT_ROOT / "output" / "data_quality_state" / "pending_sql"


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--dir", type=Path, action="append",
                        help="pending_sql directory (repeatable); default: the monitor mirror and the local data-quality directory")
    parser.add_argument("--mark-applied", type=Path, metavar="FILE",
                        help="register an executed SQL file locally and, for monitor files, on the monitor host")
    args = parser.parse_args()
    if args.mark_applied:
        source = args.mark_applied.resolve()
        name = source.parent.name if source.name == "apply.sql" else source.name
        from_monitor = MONITOR_MIRROR.resolve() in source.parents
        print(mark_applied(source))
        if from_monitor:
            remote_mark_applied(name)
        return 0
    pull_pending(MONITOR_MIRROR)
    for directory in args.dir or (MONITOR_MIRROR, DATA_QUALITY_DIR):
        entries = list_pending(directory) if directory.is_dir() else []
        print(f"{directory}: {len(entries)} pending")
        for path, header in entries:
            print(f"  {path}")
            for line in header:
                print(f"    {line}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
