"""发现批次的渲染核验：<batch-dir>/decisions.json 中等待核验的页面切成分片，在 ego-lite 中并行深度渲染
（导航失败、空白页或拦截页在新任务空间中重新渲染），可见文本未能证实时自动建立 OCR 隧道识别图片文字，
结论写入 render/verdicts.json 并写回 decisions.json。多次渲染仍不可用的页面保持 add_with_review，再次运行时重新渲染。
"""
from __future__ import annotations

import argparse
import json
from pathlib import Path

from utils.discovery_batch import DISCOVERY_DIR
from utils.render_pages import review_batch


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--batch-dir", type=Path, default=DISCOVERY_DIR)
    parser.add_argument("--shards", type=int, default=4, help="并行渲染的分片数")
    args = parser.parse_args()
    print(json.dumps(review_batch(args.batch_dir, shards=args.shards)))
    return 0
