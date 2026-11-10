"""合并重复促销与 hub 子页面：

  plan [--snapshot F]  读取只读快照（未给出时自动执行 read.sql 生成 snapshot.json），
                       生成 plan.json（抓取 hub 页面标题）
  apply-sql --plan F   把计划渲染为 apply.sql（单个 DO 块），经 MCP 执行
  verify --plan F      执行后自动重新读取快照（计划目录下 after/），核对库中结果与计划逐项一致
"""
from __future__ import annotations

import argparse
import json
from pathlib import Path

from utils import pipeline_db
from utils.ego_browser import fetch_page_title
from utils.pipeline_db import load_snapshot, write_text
from utils.pipeline_run import PROJECT_ROOT
from utils.promotion_merge import SNAPSHOT_TABLES, build_plan, dump_plan, render_apply, verify_applied

OUTPUT_DIR = PROJECT_ROOT / "output" / "pipeline" / "merge_promotions"


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = parser.add_subparsers(dest="step", required=True)
    sub.add_parser("plan").add_argument("--snapshot", type=Path)
    sub.add_parser("apply-sql").add_argument("--plan", type=Path, required=True)
    sub.add_parser("verify").add_argument("--plan", type=Path, required=True)
    args = parser.parse_args()

    if args.step == "verify":
        after = pipeline_db.fresh_snapshot(args.plan.parent / "after", SNAPSHOT_TABLES)
        problems = verify_applied(json.loads(args.plan.read_text()), load_snapshot(after, SNAPSHOT_TABLES))
        print("\n".join(problems) or "verified: database matches plan")
        return 1 if problems else 0
    if args.step == "plan":
        snapshot = args.snapshot or pipeline_db.fresh_snapshot(pipeline_db.run_dir(OUTPUT_DIR), SNAPSHOT_TABLES)
        plan = build_plan(load_snapshot(snapshot, SNAPSHOT_TABLES), fetch_page_title)
        out = write_text(snapshot.parent / "plan.json", dump_plan(plan))
        ready = sum(g["status"] == "ready" for g in plan["groups"])
        print(f"{out}\ngroups={len(plan['groups'])} ready={ready} blocked={len(plan['groups']) - ready}")
        return 0
    plan = json.loads(args.plan.read_text())
    print(write_text(args.plan.parent / "apply.sql", render_apply(plan)))
    return 0
