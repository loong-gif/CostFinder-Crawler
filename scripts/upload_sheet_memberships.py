"""把上传表 clinic_memberships 工作表中的会员行经渲染核验后写入数据库：

  plan --xlsx F [--snapshot F]     读取只读快照（未给出时自动执行 read.sql 生成 snapshot.json），筛选可写入的行，
                                   自动完成渲染核验（分片、ego-lite 渲染、可见性清单），写 plan.json（entries 与 rejected 及原因）
  apply-sql --plan F [--batch-size N]   核对同目录可见性清单后渲染 apply_01.sql ...（每批一个 DO 块），经 MCP 执行
"""
from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path

from utils import pipeline_db
from utils.membership_sheet_upload import (
    SNAPSHOT_TABLES, build_plan, read_sheet_business_names, read_sheet_rows, render_apply)
from utils.pipeline_db import load_snapshot, write_text
from utils.pipeline_run import PROJECT_ROOT
from utils.visibility_manifest import plan_with_visibility

OUTPUT_DIR = PROJECT_ROOT / "output" / "pipeline" / "upload_sheet_memberships"


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = parser.add_subparsers(dest="step", required=True)
    plan_parser = sub.add_parser("plan")
    plan_parser.add_argument("--snapshot", type=Path)
    plan_parser.add_argument("--xlsx", type=Path, required=True)
    apply_parser = sub.add_parser("apply-sql")
    apply_parser.add_argument("--plan", type=Path, required=True)
    apply_parser.add_argument("--batch-size", type=int, default=60, help="每个 DO 块的会员行数")
    args = parser.parse_args()

    if args.step == "plan":
        snapshot_path = args.snapshot or pipeline_db.fresh_snapshot(pipeline_db.run_dir(OUTPUT_DIR), SNAPSHOT_TABLES)
        snapshot = load_snapshot(snapshot_path, SNAPSHOT_TABLES)
        rows, names = read_sheet_rows(args.xlsx), read_sheet_business_names(args.xlsx)
        plan, _ = plan_with_visibility(snapshot_path.parent, lambda manifest: build_plan(rows, snapshot, names, manifest))
        out = write_text(snapshot_path.parent / "plan.json", json.dumps(plan, ensure_ascii=False, indent=2))
        print(f"{out}\nentries={len(plan['entries'])} rejected={len(plan['rejected'])}")
        return 0
    plan = json.loads(args.plan.read_text())
    manifest = json.loads((args.plan.parent / "visibility_manifest.json").read_text())
    if hashlib.sha256(json.dumps(manifest, sort_keys=True).encode()).hexdigest() != plan["visibility_manifest_sha256"]:
        raise RuntimeError("visibility_manifest_changed: the manifest differs from the one used for planning")
    entries = plan["entries"]
    for start in range(0, len(entries), args.batch_size):
        batch = {**plan, "entries": entries[start:start + args.batch_size]}
        name = f"apply_{start // args.batch_size + 1:02d}.sql"
        print(write_text(args.plan.parent / name, render_apply(batch)))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
