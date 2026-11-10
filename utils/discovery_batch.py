"""页面发现批次目录内各阶段文件的位置。所有命令只通过这里取路径，命令之间不需要人手传递或改名。

  discovered.json   discover-pages 的结果（含自动重试过的抓取失败页面）
  decisions.json    review-pages 的 add / skip 决策；render-pages 写回渲染核验结论；crawl 与 extract-pages 读取
  render/           render-pages 的分片、渲染输出、OCR 图片与 verdicts.json
  snapshot/         只读快照查询 read.sql 与 snapshot.json
"""
from __future__ import annotations

from pathlib import Path

from utils.pipeline_run import PROJECT_ROOT

DISCOVERY_DIR = PROJECT_ROOT / "state" / "discovery"


def discovered_path(batch: Path) -> Path:
    return Path(batch) / "discovered.json"


def decisions_path(batch: Path) -> Path:
    return Path(batch) / "decisions.json"


def render_dir(batch: Path) -> Path:
    return Path(batch) / "render"


def snapshot_dir(batch: Path) -> Path:
    return Path(batch) / "snapshot"


DECISIONS = decisions_path(DISCOVERY_DIR)
