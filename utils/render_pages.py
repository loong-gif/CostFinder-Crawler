"""render-pages：发现批次中等待渲染核验的页面在 ego-lite 中深度渲染，按展开后的可见文本与图片 OCR 判定，结论写回决策。

  verified          证据行在渲染后可见（正文或图片文字）：decision=add
  not_visible       页面正常渲染但证据行不可见：decision=skip
  page_unavailable  多次渲染仍导航失败、空白或被拦截：保留 add_with_review，下次运行再渲染
"""
from __future__ import annotations

import collections
import json
from pathlib import Path
from typing import Any, Callable, Dict, List, Mapping, Tuple

from utils.discovery_batch import decisions_path, render_dir
from utils.ego_render import ShardRunner, render_failure, render_pages, run_shard
from utils.paddle_ocr_markdown import ocr_tunnel
from utils.pipeline_db import write_text
from utils.render_review import build_deep_items, deep_verdict, ocr_image

ImageReader = Callable[[str], Dict[str, str]]


def text_verdicts(items: List[Mapping[str, Any]], records: Mapping[str, Mapping[str, Any]]
                  ) -> Tuple[Dict[str, Dict[str, Any]], List[str]]:
    """按渲染后的可见文本判定；返回结论与正文未能证实、页面上有图片、需要 OCR 的网址。"""
    verdicts: Dict[str, Dict[str, Any]] = {}
    needs_ocr: List[str] = []
    for item in items:
        record = records.get(item["url"])
        failure = render_failure(record)
        if failure:
            verdicts[item["url"]] = {"verdict": "page_unavailable", "reason": failure}
            continue
        result = record["result"]
        verdict = deep_verdict(item["claims"], result["texts"])
        verdict.update({"controls": result["controls"], "clicked": result["clicked"],
                        "visible_text_nodes": len(result["texts"]), "images": len(result["images"]),
                        "checked_at": record["checked_at"], "final_url": record.get("final_url")})
        verdicts[item["url"]] = verdict
        if verdict["verdict"] != "verified" and result["images"]:
            needs_ocr.append(item["url"])
    return verdicts, needs_ocr


def image_verdicts(verdicts: Dict[str, Dict[str, Any]], urls: List[str], items: List[Mapping[str, Any]],
                   records: Mapping[str, Mapping[str, Any]], read_image: ImageReader) -> None:
    """对正文未能证实的页面识别可见图片中的文字，连同正文重新判定。"""
    claims = {item["url"]: item["claims"] for item in items}
    for url in urls:
        result = records[url]["result"]
        image_texts = [read["text"] for read in (read_image(image["url"]) for image in result["images"]) if "text" in read]
        verdict = deep_verdict(claims[url], result["texts"], image_texts)
        verdicts[url].update({**verdict, "images_read": len(image_texts)})


def apply_render_verdicts(decisions: List[Dict[str, Any]], verdicts: Mapping[str, Mapping[str, Any]]) -> None:
    """渲染核验结论写回等待核验的页面；有效页面直接加入。审查已跳过的记录（重复证据、单店页面上限等）不因渲染可见而重新加入。"""
    for record in decisions:
        verdict = verdicts.get(record["url"])
        if verdict and record["decision"] == "add_with_review" and record.get("status") == "needs_review":
            record["render_review_deep"] = {k: verdict.get(k) for k in
                                            ("verdict", "reason", "source", "controls", "clicked", "images_read")}
            if verdict["verdict"] == "verified":
                record["decision"] = "add"
                record.setdefault("reasons", []).append("render_verified_after_expanding")
            elif verdict["verdict"] == "not_visible":
                record["decision"] = "skip"
                record.setdefault("reasons", []).append("not_visible_after_expanding")
        elif record["decision"] == "add_with_review" and record.get("status") == "valid":
            record["decision"] = "add"
            record.setdefault("reasons", []).append("valid_page_concrete_offer")
    waiting = [r["url"] for r in decisions if r["decision"] == "add_with_review"
               and (verdicts.get(r["url"]) or {}).get("verdict") != "page_unavailable"]
    assert not waiting, waiting


def review_batch(batch: Path, *, shards: int = 4, run: ShardRunner = run_shard) -> Dict[str, int]:
    """渲染批次中全部等待核验的页面，写 render/verdicts.json 并把结论写回 decisions.json；返回各决策的数量。"""
    payload = json.loads(decisions_path(batch).read_text())
    items = build_deep_items(payload["decisions"])
    work = render_dir(batch)
    records = render_pages(items, work, name=Path(batch).name, shards=shards, run=run)
    verdicts, needs_ocr = text_verdicts(items, records)
    if needs_ocr:
        image_dir = work / "images"
        image_dir.mkdir(parents=True, exist_ok=True)
        with ocr_tunnel():
            image_verdicts(verdicts, needs_ocr, items, records, lambda url: ocr_image(url, image_dir))
    write_text(work / "verdicts.json", json.dumps(verdicts, ensure_ascii=False, indent=1))
    apply_render_verdicts(payload["decisions"], verdicts)
    counts = dict(collections.Counter(r["decision"] for r in payload["decisions"]))
    payload["summary"] = counts
    write_text(decisions_path(batch), json.dumps(payload, ensure_ascii=False, indent=2) + "\n")
    return counts
