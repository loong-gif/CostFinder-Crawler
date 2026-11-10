"""为已有促销/会员生成重新抓取用的决定文件（crawl 与 extract-pages 的输入）：

  [--promotion-id N] [--plan-id N] [--business-id N] [--snapshot F]
      选出指定的促销与会员，以及指定商家的全部 active 促销与会员；未给出 --snapshot 时自动执行只读查询
      read.sql 得到 snapshot.json。写运行目录 decisions.json：每个来源 decision=add，kind 为 promotion/membership
"""
from __future__ import annotations

import argparse
import json
from datetime import datetime, timezone
from pathlib import Path

from utils import pipeline_db
from utils.pipeline_db import load_snapshot, write_text
from utils.pipeline_run import PROJECT_ROOT

OUTPUT_DIR = PROJECT_ROOT / "output" / "pipeline" / "recrawl_sources"


def _where(id_column: str, ids: list[int], business_ids: list[int], business_filter: str) -> str:
    parts = [f"{id_column} = any(array{sorted(ids)}::bigint[])"] if ids else []
    if business_ids:
        parts.append(f"business_id = any(array{sorted(business_ids)}::bigint[]){business_filter}")
    return " or ".join(f"({part})" for part in parts) or "false"


def _tables(promotion_ids: list[int], plan_ids: list[int], business_ids: list[int]) -> dict:
    # clinic_memberships 没有 is_active：商家的会员全部重新抓取
    return {
        "promotions": ("clinic_promotions", "promotion_id, business_id, source_url",
                       _where("promotion_id", promotion_ids, business_ids, " and is_active")),
        "memberships": ("clinic_memberships", "plan_id, business_id, source_url",
                        _where("plan_id", plan_ids, business_ids, "")),
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--promotion-id", type=int, action="append", default=[])
    parser.add_argument("--plan-id", type=int, action="append", default=[])
    parser.add_argument("--business-id", type=int, action="append", default=[])
    parser.add_argument("--snapshot", type=Path, help="已保存的 snapshot.json；默认立即读取")
    args = parser.parse_args()
    if not (args.promotion_id or args.plan_id or args.business_id):
        parser.error("give --promotion-id, --plan-id or --business-id")

    tables = _tables(args.promotion_id, args.plan_id, args.business_id)
    run_dir = OUTPUT_DIR / datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    snapshot = load_snapshot(args.snapshot or pipeline_db.fresh_snapshot(run_dir, tables), tables)
    decisions = [{"url": r["source_url"], "business_id": r["business_id"], "kind": "promotion", "decision": "add"}
                 for r in snapshot["promotions"]]
    decisions += [{"url": r["source_url"], "business_id": r["business_id"], "kind": "membership", "decision": "add"}
                  for r in snapshot["memberships"]]
    print(write_text(run_dir / "decisions.json", json.dumps({"decisions": decisions}, indent=1)))
    return 0
