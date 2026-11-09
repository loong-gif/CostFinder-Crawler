from __future__ import annotations

import json
from pathlib import Path

from utils.extract_pages_run import plan_with_visibility, render_command

URL = "https://glow.example/specials"
CARD = "Botox Bonus\nSave 20% on Botox."
TEXT = "Botox Bonus Save 20% on Botox. " + "Glow Clinic offers injectables and facials in Austin. " * 10


def test_one_call_chains_plan_shard_render_manifest_and_final_plan(tmp_path):
    seen_manifests = []

    def plan_once(manifest):
        seen_manifests.append(manifest)
        entry = {"kind": "promotion", "action": "insert", "business_id": 1, "source_url": URL, "content": [CARD]}
        return {"visibility_manifest_sha256": None if manifest is None else "x", "entries": [entry]}

    def render(shard_path: Path, out_path: Path) -> None:
        # 代替 ego-lite 渲染：按渲染脚本的输出格式写出页面可见文本
        shard = json.loads(shard_path.read_text())
        out_path.write_text(json.dumps({"results": [
            {"url": item["url"], "checked_at": "t", "http_status": 200, "result": {"texts": [TEXT], "images": []}}
            for item in shard["items"]]}))

    plan, manifest = plan_with_visibility(tmp_path, plan_once, render=render)
    assert seen_manifests[0] is None and seen_manifests[1] == manifest
    assert manifest[URL]["claimResults"][0]["status"] == "visible_match"
    assert json.loads((tmp_path / "visibility_manifest.json").read_text()) == manifest
    assert (tmp_path / "visibility_shard.json").exists() and (tmp_path / "visibility_render.json").exists()
    assert plan["visibility_manifest_sha256"] == "x"


def test_render_runs_the_deep_render_script():
    command = render_command(Path("/r/shard.json"), Path("/r/render.json"))
    assert command[0] == "sh" and command[1].endswith("scripts/run_render_review_deep.sh")
    assert command[2:] == ["/r/shard.json", "/r/render.json", "90000"]
