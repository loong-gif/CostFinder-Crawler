"""执行任一命令生成的只读快照查询 read.sql（Supabase CLI --linked），结果写入同目录 snapshot.json。"""
from __future__ import annotations

import argparse
from pathlib import Path

from utils.pipeline_db import read_snapshot


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("read_sql", type=Path)
    args = parser.parse_args()
    print(read_snapshot(args.read_sql))
    return 0
