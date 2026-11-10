"""列出待执行的 SQL 文件及其头部摘要；--apply 经 Supabase MCP 执行；--mark-applied 登记已执行的文件。

监控主机上的无人值守任务只生成 SQL 文件，不写库。列出前经 ssh 自动把监控主机 state/pending_sql/ 里待执行的文件
拉回第一个目录（自托管 Firecrawl 故障期间生成的文件不列出，见 utils.pending_sql.FIRECRAWL_OUTAGES）。
--apply 用钥匙串中的可写令牌经托管 Supabase MCP 的 execute_sql 按文件名顺序执行拉回的文件，文件自身报错的移入
rejected/ 并继续，只读令牌或非 SQL 报错时停止（见 utils.pending_sql_apply）；--install-schedule 安装每小时第 50 分钟运行 --apply 的 launchd 定时任务。
--mark-applied 把文件移入 applied/，拉回的文件同时在监控主机上移入 applied/，下一次 monitor 运行时事件即完成。
"""
from __future__ import annotations

import argparse
import asyncio
from pathlib import Path

from utils.monitor_host import BASE, HOST, mark_applied_remote, pull_pending
from utils.pending_sql import list_pending, mark_applied
from utils.pending_sql_apply import apply_pulled, install_plist, keychain_token, load_agent, supabase_session
from utils.pipeline_run import PROJECT_ROOT

PULL_DIR = PROJECT_ROOT / "output" / "self_hosted_monitor_state" / "pending_sql"
DEFAULT_DIRS = (PULL_DIR, PROJECT_ROOT / "output" / "data_quality_state" / "pending_sql")


def _apply(pulled_dir: Path, base: str, host: str) -> int:
    token = keychain_token()

    async def run() -> dict:
        async with supabase_session(token) as client:
            return await apply_pulled(pulled_dir, client, base=base, host=host)
    report = asyncio.run(run())
    for path in report["applied"]:
        print(f"applied {path}")
    for path, guard in report["rejected"]:
        print(f"rejected {path} ({guard})")
    if report["stopped"]:
        print(f"stopped {report['stopped']}\n{report['error']}")
    print(f"summary: applied {len(report['applied'])}, rejected {len(report['rejected'])}, "
          f"stopped {int(bool(report['stopped']))}")
    return 1 if report["stopped"] else 0


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--dir", type=Path, action="append",
                        help="pending_sql directory (repeatable); the first one receives the files pulled from the "
                             "monitor host; default: the local monitor and data-quality state directories")
    parser.add_argument("--mark-applied", type=Path, metavar="FILE",
                        help="move an executed SQL file into applied/ (on the monitor host too when it was pulled from there)")
    parser.add_argument("--apply", action="store_true",
                        help="pull, then execute the pulled files through the Supabase MCP in filename order and mark "
                             "them applied; a file failing its own SQL checks moves to rejected/ and the run continues; "
                             "a read-only token or a non-SQL failure stops the run (exit code 1)")
    parser.add_argument("--install-schedule", action="store_true",
                        help="install and load the launchd agent that runs --apply hourly at minute 50")
    parser.add_argument("--host", default=HOST)
    parser.add_argument("--remote-base", default=BASE)
    args = parser.parse_args()
    directories = args.dir or list(DEFAULT_DIRS)
    pulled_dir = directories[0].resolve()
    if args.install_schedule:
        path = install_plist()
        load_agent(path)
        print(f"loaded {path}")
        return 0
    if args.apply:
        return _apply(pulled_dir, args.remote_base, args.host)
    if args.mark_applied:
        path = args.mark_applied.resolve()
        if path.is_relative_to(pulled_dir):
            # 先移动服务器上的文件；失败即终止，本地文件保持待执行
            print(mark_applied_remote(str(path.relative_to(pulled_dir)), base=args.remote_base, host=args.host))
        print(mark_applied(path))
        return 0
    for path in pull_pending(directories[0], base=args.remote_base, host=args.host):
        print(f"pulled {path}")
    for directory in directories:
        entries = list_pending(directory) if directory.is_dir() else []
        print(f"{directory}: {len(entries)} pending")
        for path, header in entries:
            print(f"  {path}")
            for line in header:
                print(f"    {line}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
