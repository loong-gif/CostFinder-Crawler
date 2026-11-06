"""extract-pages 计划的渲染分流。

  --plan F [--raw-dir D]   读取没有可见性清单时生成的 plan.json 与原始爬取结果，
                           在计划同目录写 triage.json：每条促销的去向（reject / render / markdown_traceable）
                           与需要渲染的页面清单 render_pages
"""
from __future__ import annotations

import argparse
import json
from pathlib import Path

from utils.page_extraction_snapshot import latest_scrapes
from utils.pipeline_db import write_text
from utils.raw_file_store import RawFileStore
from utils.render_triage import triage_plan


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--plan", type=Path, required=True)
    parser.add_argument("--raw-dir", type=Path, help="原始爬取结果目录，默认 COSTFINDER_RAW_DIR")
    args = parser.parse_args()
    store = RawFileStore(args.raw_dir) if args.raw_dir else RawFileStore()
    scrapes = latest_scrapes(store.fetch_rows("firecrawl_scrape_raw", "*", filters={"success": "eq.true"}))
    triage = triage_plan(json.loads(args.plan.read_text()), scrapes)
    out = write_text(args.plan.parent / "triage.json", json.dumps(triage, ensure_ascii=False, indent=2))
    print(f"{out}\n{json.dumps(triage['summary'])} render_pages={len(triage['render_pages'])}")
    return 0
