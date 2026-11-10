"""从 offer 识别 service，回填 clinic_services 与 promo_offer_items.service_id：

  plan [--business-id N ...] [--snapshot F] [--state-dir D]
                            读取只读快照（未给出时自动执行 read.sql 生成 snapshot.json），
                            生成 plan.json（需要时调用 LLM）；quality 报告（D/data_quality/report.json）中
                            去向为 relink 的已关联 item 一并重新判定：一致保留（keep），判出另一服务改关联，
                            判不出置 NULL（unlink）；LLM 确认保留的映射按"offer 原文 + item 名称"
                            写入 service_mapping_confirmations.json，quality 不再报出
  apply-sql --plan F        把计划渲染为 apply.sql（单个 DO 块），经 MCP 执行
"""
from __future__ import annotations

import argparse
import json
from pathlib import Path

from dotenv import load_dotenv

from utils import pipeline_db
from utils.data_quality_audit import relink_items
from utils.monitor_price_store import read_json
from utils.offer_extraction_llm import build_client_from_env
from utils.pipeline_db import load_snapshot, write_text
from utils.pipeline_run import PROJECT_ROOT
from utils.service_mapping_confirmations import record_confirmations
from utils.service_linking import SNAPSHOT_TABLES, build_plan, dump_plan, render_apply, snapshot_tables

OUTPUT_DIR = PROJECT_ROOT / "output" / "pipeline" / "link_services"


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = parser.add_subparsers(dest="step", required=True)
    plan_parser = sub.add_parser("plan")
    plan_parser.add_argument("--business-id", type=int, action="append", help="只处理这些商家（可重复）")
    plan_parser.add_argument("--snapshot", type=Path)
    plan_parser.add_argument("--state-dir", type=Path, default=PROJECT_ROOT / "output" / "data_quality_state",
                             help="数据质量状态目录（读取其中 quality 报告的 relink item）")
    apply_parser = sub.add_parser("apply-sql")
    apply_parser.add_argument("--plan", type=Path, required=True)
    apply_parser.add_argument("--batch-size", type=int,
                              help="每批最多关联的 item 数（按店铺整体分批）；写出 apply_01.sql、apply_02.sql ...")
    args = parser.parse_args(argv)

    if args.step == "plan":
        load_dotenv(PROJECT_ROOT / ".env")
        client = build_client_from_env()
        if client is None:
            raise RuntimeError("LLM client is not configured (LLM_API_KEY / LLM_MODEL / LLM_API_URL)")
        # quality 交来重新判定的 item（service_scope_evidence 规则，去向 relink）
        relink = frozenset(relink_items(read_json(args.state_dir / "data_quality" / "report.json", {})))
        snapshot = args.snapshot or pipeline_db.fresh_snapshot(pipeline_db.run_dir(OUTPUT_DIR),
                                                               snapshot_tables(args.business_id))
        plan = build_plan(load_snapshot(snapshot, SNAPSHOT_TABLES), client, relink)
        out = write_text(snapshot.parent / "plan.json", dump_plan(plan))
        record_confirmations(plan["confirmed_mappings"])
        print(f"{out}\n{json.dumps(plan['counts'])} services_to_create={len(plan['services_to_create'])} "
              f"confirmed_mappings={len(plan['confirmed_mappings'])}")
        return 0
    plan = json.loads(args.plan.read_text())
    if args.batch_size is None:
        print(write_text(args.plan.parent / "apply.sql", render_apply(plan)))
        return 0
    # 按店铺分批：同一店铺新建的 service 与关联它们的 item 留在同一批
    by_business: dict[int, list[dict]] = {}
    for entry in plan["items"]:
        if entry["action"] in ("link", "create_and_link", "unlink"):
            by_business.setdefault(entry["business_id"], []).append(entry)
    batches: list[list[int]] = [[]]
    size = 0
    for business_id, entries in by_business.items():
        if size and size + len(entries) > args.batch_size:
            batches.append([])
            size = 0
        batches[-1].append(business_id)
        size += len(entries)
    for number, businesses in enumerate(batches, start=1):
        batch = {**plan, "items": [e for b in businesses for e in by_business[b]],
                 "services_to_create": [s for s in plan["services_to_create"] if s["business_id"] in businesses]}
        print(write_text(args.plan.parent / f"apply_{number:02d}.sql", render_apply(batch)))
    return 0
