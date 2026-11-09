"""extract-pages 计划的渲染可见性清单：

  --plan P [--shards N]   把计划中各页的内容行写成分片，在 ego-lite 中深度渲染（导航失败、空白页或拦截页重新渲染），
                          有促销图片时自动建立 OCR 隧道识别，在计划同目录写 visibility_manifest.json

数据质量核验单独使用本命令；extract-pages plan 与 upload-sheet-memberships plan 经
utils.visibility_manifest.plan_with_visibility 调用同一个 run_manifest。
"""
from __future__ import annotations

import argparse
import json
from pathlib import Path

from utils.visibility_manifest import run_manifest


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--plan", type=Path, required=True)
    parser.add_argument("--shards", type=int, default=4, help="并行渲染的分片数")
    args = parser.parse_args()
    path = run_manifest(args.plan, shards=args.shards)
    print(path)
    for url, page in json.loads(path.read_text()).items():
        visible = sum(c["status"] == "visible_match" for c in page["claimResults"])
        print(f"{page['status']} {visible}/{len(page['claimResults'])} {url}")
    return 0
