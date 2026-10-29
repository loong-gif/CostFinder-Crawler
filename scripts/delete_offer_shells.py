"""删除没有 item、没有用户数据引用的 inactive 空壳 offer。数据库读写经 Supabase MCP：

  read-sql            生成只读快照查询 read.sql（read-snapshot 执行后得到 snapshot.json）
  plan --snapshot F   列出可删除与被用户数据引用而保留的空壳，写 plan.json
  apply-sql --plan F  渲染 apply.sql（单个 DO 块），经 MCP 执行
"""
from __future__ import annotations

import argparse
import json
from datetime import datetime, timezone
from pathlib import Path

from utils.offer_shells import SNAPSHOT_TABLES, build_plan, render_apply
from utils.pipeline_db import load_snapshot, snapshot_query, write_text
from utils.pipeline_run import PROJECT_ROOT

OUTPUT_DIR = PROJECT_ROOT / "output" / "pipeline" / "delete_offer_shells"


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = parser.add_subparsers(dest="step", required=True)
    sub.add_parser("read-sql")
    plan_parser = sub.add_parser("plan")
    plan_parser.add_argument("--snapshot", type=Path, required=True)
    apply_parser = sub.add_parser("apply-sql")
    apply_parser.add_argument("--plan", type=Path, required=True)
    args = parser.parse_args()

    if args.step == "read-sql":
        run_dir = OUTPUT_DIR / datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
        print(write_text(run_dir / "read.sql", snapshot_query(SNAPSHOT_TABLES)))
        return 0
    if args.step == "plan":
        plan = build_plan(load_snapshot(args.snapshot, SNAPSHOT_TABLES))
        out = write_text(args.snapshot.parent / "plan.json", json.dumps(plan, ensure_ascii=False, indent=2))
        print(f"{out}\ndelete={len(plan['delete'])} blocked={len(plan['blocked'])}")
        return 0
    plan = json.loads(args.plan.read_text())
    print(write_text(args.plan.parent / "apply.sql", render_apply(plan)))
    return 0
