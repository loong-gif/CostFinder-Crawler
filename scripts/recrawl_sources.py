"""为已有促销/会员生成重新抓取用的决定文件（crawl 与 extract-pages 的输入）：

  read-sql --promotion-id N [--plan-id N]   只读快照查询 read.sql（read-snapshot 执行后得到 snapshot.json）
  decisions --snapshot F                     写同目录 decisions.json：每个来源 decision=add，kind 为 promotion/membership
"""
from __future__ import annotations

import argparse
import json
from datetime import datetime, timezone
from pathlib import Path

from utils.pipeline_db import load_snapshot, snapshot_query, write_text
from utils.pipeline_run import PROJECT_ROOT

OUTPUT_DIR = PROJECT_ROOT / "output" / "pipeline" / "recrawl_sources"


def _tables(promotion_ids: list[int], plan_ids: list[int]) -> dict:
    return {
        "promotions": ("clinic_promotions", "promotion_id, business_id, source_url",
                       f"promotion_id = any(array{sorted(promotion_ids)}::bigint[])" if promotion_ids else "false"),
        "memberships": ("clinic_memberships", "plan_id, business_id, source_url",
                        f"plan_id = any(array{sorted(plan_ids)}::bigint[])" if plan_ids else "false"),
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = parser.add_subparsers(dest="step", required=True)
    read = sub.add_parser("read-sql")
    read.add_argument("--promotion-id", type=int, action="append", default=[])
    read.add_argument("--plan-id", type=int, action="append", default=[])
    decide = sub.add_parser("decisions")
    decide.add_argument("--snapshot", type=Path, required=True)
    args = parser.parse_args()

    if args.step == "read-sql":
        run_dir = OUTPUT_DIR / datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
        tables = _tables(args.promotion_id, args.plan_id)
        write_text(run_dir / "selection.json", json.dumps({"promotion_ids": args.promotion_id, "plan_ids": args.plan_id}))
        print(write_text(run_dir / "read.sql", snapshot_query(tables)))
        return 0
    selection = json.loads((args.snapshot.parent / "selection.json").read_text())
    snapshot = load_snapshot(args.snapshot, _tables(selection["promotion_ids"], selection["plan_ids"]))
    decisions = [{"url": r["source_url"], "business_id": r["business_id"], "kind": "promotion", "decision": "add"}
                 for r in snapshot["promotions"]]
    decisions += [{"url": r["source_url"], "business_id": r["business_id"], "kind": "membership", "decision": "add"}
                  for r in snapshot["memberships"]]
    print(write_text(args.snapshot.parent / "decisions.json", json.dumps({"decisions": decisions}, indent=1)))
    return 0
