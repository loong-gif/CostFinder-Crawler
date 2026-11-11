"""在 ego-lite 中深度渲染一批页面（scripts/render_review_deep.js：展开折叠面板与标签页，滚动加载，收集可见文本与图片）。

页面切成分片并行渲染，分片与输出文件名由代码确定。导航失败、空白页或拦截页在新的任务空间中重新渲染，
最多 MAX_ATTEMPTS 次；仍失败的页面由调用方保留，下次运行再渲染，不当作页面不可见。
"""
from __future__ import annotations

import json
import subprocess
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Any, Callable, Dict, List, Mapping, Optional

from utils.ego_browser import ego_command
from utils.page_title import is_block_page
from utils.pipeline_run import PROJECT_ROOT
from utils.render_review import MAX_ATTEMPTS, shard

RENDER_SCRIPT = PROJECT_ROOT / "scripts" / "render_review_deep.js"
RENDER_TIMEOUT_MS = 90000
MIN_RENDERED_CHARS = 300

ShardRunner = Callable[[Path, Path, int], None]


def shard_command(shard_path: Path, out_path: Path, timeout_ms: int) -> tuple[list[str], str]:
    """ego-browser nodejs 的参数与标准输入：首行注入分片与输出的绝对路径（ego 的 Node 进程工作目录不同），其后是渲染脚本。"""
    command = ego_command()
    paths = [str(Path(path).absolute()) for path in (shard_path, out_path)]
    if command.lower().endswith(".exe"):
        paths = [subprocess.check_output(["wslpath", "-w", path], text=True).strip() for path in paths]
    config = {"shard": paths[0], "out": paths[1], "timeout": timeout_ms}
    return [command, "nodejs"], f"globalThis.RR = {json.dumps(config)};\n" + RENDER_SCRIPT.read_text()


def run_shard(shard_path: Path, out_path: Path, timeout_ms: int) -> None:
    argv, script = shard_command(shard_path, out_path, timeout_ms)
    subprocess.run(argv, input=script, text=True, encoding="utf-8", check=True)


def render_failure(record: Optional[Mapping[str, Any]]) -> Optional[str]:
    """渲染没有得到可用页面时的原因：未渲染、导航失败、空白页或拦截页；正常渲染返回 None。"""
    if record is None:
        return "not_rendered"
    if record.get("error"):
        return str(record["error"])
    texts = record["result"]["texts"]
    if sum(len(t) for t in texts) < MIN_RENDERED_CHARS or is_block_page("\n".join(texts)):
        return "blank_or_blocked_render"
    return None


def _render_once(items: List[Dict[str, Any]], work_dir: Path, name: str, shards: int, timeout_ms: int,
                 run: ShardRunner) -> Dict[str, Dict[str, Any]]:
    work_dir.mkdir(parents=True, exist_ok=True)
    jobs = []
    for index, part in enumerate(shard(items, shards), start=1):
        shard_path, out_path = work_dir / f"shard{index}.json", work_dir / f"out{index}.json"
        shard_path.write_text(json.dumps({"name": f"{name}-{index}", "items": part}, ensure_ascii=False, indent=1))
        jobs.append((shard_path, out_path))
    with ThreadPoolExecutor(max_workers=max(1, len(jobs))) as pool:
        list(pool.map(lambda job: run(job[0], job[1], timeout_ms), jobs))
    return {record["url"]: record for _, out_path in jobs
            for record in json.loads(out_path.read_text())["results"]}


def render_pages(items: List[Dict[str, Any]], work_dir: Path, *, name: str, shards: int = 4,
                 timeout_ms: int = RENDER_TIMEOUT_MS, attempts: int = MAX_ATTEMPTS,
                 run: ShardRunner = run_shard) -> Dict[str, Dict[str, Any]]:
    """渲染全部页面，没有得到可用页面的再渲染，返回 {url: 渲染记录}（最后一次渲染的结果）。"""
    records: Dict[str, Dict[str, Any]] = {}
    pending = items
    for attempt in range(1, attempts + 1):
        if not pending:
            break
        records.update(_render_once(pending, work_dir / f"attempt{attempt}", f"{name}-{attempt}", shards, timeout_ms,
                                    run))
        pending = [item for item in pending if render_failure(records.get(item["url"]))]
    return records
