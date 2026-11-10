"""按入库规则复查存量 active 促销，违规行改为 inactive：

  plan [--snapshot F]  读取只读快照（未给出时自动执行 read.sql 生成 snapshot.json），
                       列出违反 active 规则的促销及原因，写 plan.json
  apply-sql --plan F   渲染 apply.sql（单个 DO 块），经 MCP 执行
"""
from __future__ import annotations

import argparse
import json
from pathlib import Path

from utils import pipeline_db
from utils.pipeline_db import load_snapshot, write_text
from utils.pipeline_run import PROJECT_ROOT
from utils.promotion_enforcement import SNAPSHOT_TABLES, build_plan, render_apply

OUTPUT_DIR = PROJECT_ROOT / "output" / "pipeline" / "enforce_promotion_rules"


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = parser.add_subparsers(dest="step", required=True)
    sub.add_parser("plan").add_argument("--snapshot", type=Path)
    sub.add_parser("apply-sql").add_argument("--plan", type=Path, required=True)
    args = parser.parse_args()

    if args.step == "plan":
        snapshot = args.snapshot or pipeline_db.fresh_snapshot(pipeline_db.run_dir(OUTPUT_DIR), SNAPSHOT_TABLES)
        plan = build_plan(load_snapshot(snapshot, SNAPSHOT_TABLES))
        out = write_text(snapshot.parent / "plan.json", json.dumps(plan, ensure_ascii=False, indent=2))
        print(f"{out}\nactive={plan['active_promotions']} violations={len(plan['violations'])}")
        return 0
    plan = json.loads(args.plan.read_text())
    print(write_text(args.plan.parent / "apply.sql", render_apply(plan)))
    return 0
