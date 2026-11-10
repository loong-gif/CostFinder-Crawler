"""停用 campaign_end_date 已过的促销（下游 offer 由数据库触发器一并停用）。

生成 apply.sql（固定语句，判定在数据库函数内），经 Supabase MCP 执行；监控主机上的每日任务只生成该文件，
由 pending-sql 拉回后执行。
"""
from __future__ import annotations

import argparse
from datetime import datetime, timezone
from pathlib import Path

from utils.pipeline_db import write_text
from utils.pipeline_run import PROJECT_ROOT

OUTPUT_DIR = PROJECT_ROOT / "output" / "pipeline" / "deactivate_expired"
APPLY_SQL = (
    "-- deactivate-expired apply\n"
    "select promotion_id from public.fn_deactivate_expired_promotions() as promotion_id;\n"
)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--out-dir", type=Path, default=OUTPUT_DIR)
    args = parser.parse_args()
    run_dir = args.out_dir / datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    print(write_text(run_dir / "apply.sql", APPLY_SQL))
    return 0
