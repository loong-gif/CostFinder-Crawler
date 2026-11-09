from __future__ import annotations

import json
import subprocess
from pathlib import Path
from typing import Any, Callable, Mapping

from utils.pipeline_db import write_text
from utils.pipeline_run import PROJECT_ROOT
from utils.visibility_manifest import build_manifest, build_shard

RENDER_SCRIPT = PROJECT_ROOT / "scripts" / "run_render_review_deep.sh"
RENDER_TIMEOUT_MS = 90000


def render_command(shard_path: Path, out_path: Path) -> list[str]:
    return ["sh", str(RENDER_SCRIPT), str(shard_path), str(out_path), str(RENDER_TIMEOUT_MS)]


def render_shard(shard_path: Path, out_path: Path) -> None:
    """ego-lite 深度渲染核验分片中的每个页面，结果写入 out_path。"""
    subprocess.run(render_command(shard_path, out_path), check=True)


def plan_with_visibility(run_dir: Path, plan_once: Callable[[Mapping[str, Any] | None], dict[str, Any]], *,
                         render: Callable[[Path, Path], None] = render_shard) -> tuple[dict[str, Any], dict[str, Any]]:
    """无清单计划 → 可见性分片 → ego-lite 渲染 → 可见性清单 → 带清单计划；中间文件都写在 run_dir 下。
    LLM 回答有缓存，第二次计划不再请求 LLM。"""
    shard = build_shard(plan_once(None), run_dir.name)
    shard_path = write_text(run_dir / "visibility_shard.json", json.dumps(shard, ensure_ascii=False, indent=1))
    render_path = run_dir / "visibility_render.json"
    render(shard_path, render_path)
    manifest = build_manifest(shard, json.loads(render_path.read_text()))
    write_text(run_dir / "visibility_manifest.json", json.dumps(manifest, ensure_ascii=False, indent=1))
    return plan_once(manifest), manifest
