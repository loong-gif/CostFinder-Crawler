"""把一条 offer 里并列的多个服务拆成各自独立的 offer。数据库读写经 Supabase MCP：

  read-sql                       生成只读快照查询 read.sql（read-snapshot 执行后得到 snapshot.json）
  plan --snapshot F              按服务词典识别并列服务，生成 plan.json（不调用 LLM）
  apply-sql --plan F             渲染 apply.sql（单个 DO 块），经 MCP 执行
"""
from __future__ import annotations

import argparse
import json
from datetime import datetime, timezone
from pathlib import Path

from utils.offer_split import build_plan, render_apply, snapshot_tables
from utils.pipeline_db import load_snapshot, snapshot_query, write_text
from utils.pipeline_run import PROJECT_ROOT

OUTPUT_DIR = PROJECT_ROOT / "output" / "pipeline" / "split_offers"


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = parser.add_subparsers(dest="step", required=True)
    sub.add_parser("read-sql")
    sub.add_parser("plan").add_argument("--snapshot", type=Path, required=True)
    sub.add_parser("apply-sql").add_argument("--plan", type=Path, required=True)
    args = parser.parse_args()

    if args.step == "read-sql":
        run_dir = OUTPUT_DIR / datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
        print(write_text(run_dir / "read.sql", snapshot_query(snapshot_tables())))
        return 0
    if args.step == "plan":
        plan = build_plan(load_snapshot(args.snapshot, snapshot_tables()))
        out = write_text(args.snapshot.parent / "plan.json",
                         json.dumps(plan, ensure_ascii=False, indent=2, default=str))
        pieces = sum(len(split["pieces"]) for split in plan["splits"])
        print(f"{out}\noffers_to_split={len(plan['splits'])} new_offers={pieces} skipped={len(plan['skipped'])}")
        return 0
    plan = json.loads(args.plan.read_text())
    print(write_text(args.plan.parent / "apply.sql", render_apply(plan)))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
