"""重写 active offer 中有问题的 item_name。数据库读写经 Supabase MCP：

  read-sql                       生成只读快照查询 read.sql（read-snapshot 执行后得到 snapshot.json）
  plan --snapshot F              调用 LLM 按 item_name 的定义生成新名称，写 plan.json
  apply-sql --plan F [--batch-size N]   渲染 apply_01.sql ...（每批一个 DO 块），经 MCP 执行
"""
from __future__ import annotations

import argparse
import json
from datetime import datetime, timezone
from pathlib import Path

from dotenv import load_dotenv

from utils.item_name_rename import build_plan, render_apply, snapshot_tables
from utils.offer_extraction_llm import build_client_from_env
from utils.pipeline_db import load_snapshot, snapshot_query, write_text
from utils.pipeline_run import PROJECT_ROOT

OUTPUT_DIR = PROJECT_ROOT / "output" / "pipeline" / "rename_items"


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = parser.add_subparsers(dest="step", required=True)
    sub.add_parser("read-sql")
    sub.add_parser("plan").add_argument("--snapshot", type=Path, required=True)
    apply_parser = sub.add_parser("apply-sql")
    apply_parser.add_argument("--plan", type=Path, required=True)
    apply_parser.add_argument("--batch-size", type=int, default=50,
                              help="每个 DO 块的 item 数量；写出 apply_01.sql、apply_02.sql ...")
    args = parser.parse_args()

    if args.step == "read-sql":
        run_dir = OUTPUT_DIR / datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
        print(write_text(run_dir / "read.sql", snapshot_query(snapshot_tables())))
        return 0
    if args.step == "plan":
        load_dotenv(PROJECT_ROOT / ".env")
        client = build_client_from_env()
        if client is None:
            raise RuntimeError("LLM client not configured (LLM_API_KEY / LLM_MODEL)")
        plan = build_plan(load_snapshot(args.snapshot, snapshot_tables()), client, OUTPUT_DIR / "llm_cache")
        out = write_text(args.snapshot.parent / "plan.json", json.dumps(plan, ensure_ascii=False, indent=2))
        print(f"{out}\nrename={len(plan['entries'])} skipped={len(plan['skipped'])}")
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
