"""extract-pages 计划的渲染可见性清单：

  shard --plan P        把计划中各页的内容行写成深度渲染核验分片 visibility_shard.json
  （运行 scripts/run_render_review_deep.sh visibility_shard.json visibility_render.json 90000，ego-lite 渲染）
  build --plan P        读取分片与渲染结果，写 visibility_manifest.json，供 extract-pages plan/apply-sql 使用
"""
from __future__ import annotations

import argparse
import json
from pathlib import Path

from utils.pipeline_db import write_text
from utils.visibility_manifest import build_manifest, build_shard


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = parser.add_subparsers(dest="step", required=True)
    for step in ("shard", "build"):
        sub.add_parser(step).add_argument("--plan", type=Path, required=True)
    args = parser.parse_args()
    run_dir = args.plan.parent
    if args.step == "shard":
        shard = build_shard(json.loads(args.plan.read_text()), run_dir.name)
        print(write_text(run_dir / "visibility_shard.json", json.dumps(shard, ensure_ascii=False, indent=1)))
        print(f"pages={len(shard['items'])}")
        return 0
    manifest = build_manifest(json.loads((run_dir / "visibility_shard.json").read_text()),
                              json.loads((run_dir / "visibility_render.json").read_text()))
    print(write_text(run_dir / "visibility_manifest.json", json.dumps(manifest, ensure_ascii=False, indent=1)))
    for url, page in manifest.items():
        visible = sum(c["status"] == "visible_match" for c in page["claimResults"])
        print(f"{page['status']} {visible}/{len(page['claimResults'])} {url}")
    return 0
