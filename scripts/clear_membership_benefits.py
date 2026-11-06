"""把核验未通过的会员权益置为 NULL。数据库读写经 Supabase MCP：

  read-sql --plan-id N ...                    生成只读快照查询 read.sql（read-snapshot 执行后得到 snapshot.json）
  plan --snapshot F --reasons R               R 为 JSON 文件 {plan_id: 原因}；写 plan.json
  apply-sql --plan F                          渲染 apply.sql（单个 DO 块），经 MCP 执行
"""
from __future__ import annotations

import argparse
import json
from datetime import datetime, timezone
from pathlib import Path

from utils.membership_benefit_reset import build_plan, render_apply, snapshot_tables
from utils.pipeline_db import load_snapshot, snapshot_query, write_text
from utils.pipeline_run import PROJECT_ROOT

OUTPUT_DIR = PROJECT_ROOT / "output" / "pipeline" / "clear_membership_benefits"


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = parser.add_subparsers(dest="step", required=True)
    sub.add_parser("read-sql").add_argument("--plan-id", type=int, action="append", required=True)
    plan_parser = sub.add_parser("plan")
    plan_parser.add_argument("--snapshot", type=Path, required=True)
    plan_parser.add_argument("--reasons", type=Path, required=True)
    sub.add_parser("apply-sql").add_argument("--plan", type=Path, required=True)
    args = parser.parse_args()

    if args.step == "read-sql":
        run_dir = OUTPUT_DIR / datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
        print(write_text(run_dir / "read.sql", snapshot_query(snapshot_tables(args.plan_id))))
        return 0
    if args.step == "plan":
        reasons = {int(k): v for k, v in json.loads(args.reasons.read_text()).items()}
        plan = build_plan(load_snapshot(args.snapshot, snapshot_tables(reasons)), reasons)
        out = write_text(args.snapshot.parent / "plan.json", json.dumps(plan, ensure_ascii=False, indent=2, default=str))
        print(f"{out}\nclear={len(plan['entries'])} already_null={len(plan['already_null'])}")
        return 0
    plan = json.loads(args.plan.read_text())
    print(write_text(args.plan.parent / "apply.sql", render_apply(plan)))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
