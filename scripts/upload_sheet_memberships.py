"""把上传表 clinic_memberships 工作表中 QA 通过的会员行写入数据库。数据库读写经 Supabase MCP：

  read-sql                         生成只读快照查询 read.sql（read-snapshot 执行后得到 snapshot.json）
  plan --snapshot F --xlsx F       筛选可写入的行，写 plan.json（entries 与 rejected 及原因）
  apply-sql --plan F [--batch-size N]   渲染 apply_01.sql ...（每批一个 DO 块），经 MCP 执行
"""
from __future__ import annotations

import argparse
import json
from datetime import datetime, timezone
from pathlib import Path

from utils.membership_sheet_upload import (
    SNAPSHOT_TABLES, build_plan, read_sheet_business_names, read_sheet_rows, render_apply)
from utils.pipeline_db import load_snapshot, snapshot_query, write_text
from utils.pipeline_run import PROJECT_ROOT

OUTPUT_DIR = PROJECT_ROOT / "output" / "pipeline" / "upload_sheet_memberships"


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = parser.add_subparsers(dest="step", required=True)
    sub.add_parser("read-sql")
    plan_parser = sub.add_parser("plan")
    plan_parser.add_argument("--snapshot", type=Path, required=True)
    plan_parser.add_argument("--xlsx", type=Path, required=True)
    apply_parser = sub.add_parser("apply-sql")
    apply_parser.add_argument("--plan", type=Path, required=True)
    apply_parser.add_argument("--batch-size", type=int, default=60, help="每个 DO 块的会员行数")
    args = parser.parse_args()

    if args.step == "read-sql":
        run_dir = OUTPUT_DIR / datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
        print(write_text(run_dir / "read.sql", snapshot_query(SNAPSHOT_TABLES)))
        return 0
    if args.step == "plan":
        plan = build_plan(read_sheet_rows(args.xlsx), load_snapshot(args.snapshot, SNAPSHOT_TABLES),
                          read_sheet_business_names(args.xlsx))
        out = write_text(args.snapshot.parent / "plan.json", json.dumps(plan, ensure_ascii=False, indent=2))
        print(f"{out}\nentries={len(plan['entries'])} rejected={len(plan['rejected'])}")
        return 0
    plan = json.loads(args.plan.read_text())
    entries = plan["entries"]
    for start in range(0, len(entries), args.batch_size):
        batch = {**plan, "entries": entries[start:start + args.batch_size]}
        name = f"apply_{start // args.batch_size + 1:02d}.sql"
        print(write_text(args.plan.parent / name, render_apply(batch)))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
