from __future__ import annotations

import json
from pathlib import Path

from utils.ego_render import RENDER_SCRIPT, render_failure, render_pages, shard_command

CHROME = ["Our board-certified injectors have served the community for over ten years with natural results. " * 4]


class _Ego:
    """ego-lite 渲染脚本的替身：按 render_review_deep.js 的输出格式把分片写成 out 文件。
    outcomes[url] 依次给出每次渲染的结果（"ok"、"blocked" 或错误文本）。"""

    def __init__(self, outcomes):
        self.outcomes, self.runs = outcomes, []

    def __call__(self, shard_path: Path, out_path: Path, timeout_ms: int) -> None:
        shard = json.loads(shard_path.read_text())
        self.runs.append((shard["name"], [item["url"] for item in shard["items"]]))
        results = []
        for item in shard["items"]:
            outcome = self.outcomes[item["url"]].pop(0)
            record = {"url": item["url"], "checked_at": f"run{len(self.runs)}"}
            if outcome == "ok":
                record["result"] = {"texts": [*CHROME, "Botox $10 per unit"], "images": [], "controls": 0, "clicked": 0}
            elif outcome == "blocked":
                record["result"] = {"texts": ["Access denied"], "images": [], "controls": 0, "clicked": 0}
            else:
                record["error"] = outcome
            results.append(record)
        out_path.write_text(json.dumps({"shard": shard["name"], "results": results}))


def _items(*urls):
    return [{"id": str(i), "url": url, "claims": []} for i, url in enumerate(urls)]


def test_every_shard_is_rendered_and_the_results_are_merged_by_url(tmp_path):
    ego = _Ego({"https://a.com/x": ["ok"], "https://b.com/x": ["ok"], "https://c.com/x": ["ok"]})
    records = render_pages(_items("https://a.com/x", "https://b.com/x", "https://c.com/x"), tmp_path, name="batch",
                           shards=2, run=ego)
    assert sorted(records) == ["https://a.com/x", "https://b.com/x", "https://c.com/x"]
    assert sorted(urls for _, urls in ego.runs) == [["https://a.com/x", "https://c.com/x"], ["https://b.com/x"]]
    assert (tmp_path / "attempt1" / "shard1.json").exists() and (tmp_path / "attempt1" / "out1.json").exists()


def test_unavailable_or_blocked_pages_are_rendered_again_in_a_new_task_space(tmp_path):
    ego = _Ego({"https://a.com/x": ["ok"],
                "https://b.com/x": ["page.goto failed: net::ERR_BLOCKED_BY_CLIENT", "ok"],
                "https://c.com/x": ["blocked", "blocked", "blocked"]})
    records = render_pages(_items("https://a.com/x", "https://b.com/x", "https://c.com/x"), tmp_path, name="batch",
                           shards=1, attempts=3, run=ego)
    assert [names for names, _ in ego.runs] == ["batch-1-1", "batch-2-1", "batch-3-1"]
    assert [urls for _, urls in ego.runs] == [["https://a.com/x", "https://b.com/x", "https://c.com/x"],
                                              ["https://b.com/x", "https://c.com/x"], ["https://c.com/x"]]
    assert render_failure(records["https://a.com/x"]) is None and records["https://a.com/x"]["checked_at"] == "run1"
    assert render_failure(records["https://b.com/x"]) is None and records["https://b.com/x"]["checked_at"] == "run2"
    assert render_failure(records["https://c.com/x"]) == "blank_or_blocked_render"


def test_the_shard_runs_the_deep_render_script_in_ego_lite(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    argv, script = shard_command(Path("render/shard1.json"), Path("render/out1.json"), 90000)
    assert argv[1:] == ["nodejs"] and argv[0].endswith("ego-browser")
    config = json.loads(script.split("\n", 1)[0].removeprefix("globalThis.RR = ").rstrip(";"))
    # 相对路径转为绝对路径：ego 的 Node 进程不在本命令的工作目录中运行
    assert config == {"shard": str(Path.cwd() / "render/shard1.json"), "out": str(Path.cwd() / "render/out1.json"),
                      "timeout": 90000}
    assert script.split("\n", 1)[1] == RENDER_SCRIPT.read_text()
