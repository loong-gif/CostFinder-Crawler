from __future__ import annotations

import json

from utils.discovery_batch import decisions_path, render_dir
from utils.render_pages import apply_render_verdicts, image_verdicts, review_batch, text_verdicts

URL = "https://spa.example/specials"
CHROME = ["Our board-certified injectors have served the community for over ten years with natural results. " * 4]


def _record(url, texts, images=()):
    return {"url": url, "checked_at": "t", "final_url": url,
            "result": {"texts": [*CHROME, *texts], "images": [{"url": i} for i in images], "controls": 2, "clicked": 1}}


def test_render_verdict_settles_pages_waiting_for_it_and_keeps_unrendered_ones_waiting():
    decisions = [
        {"url": URL, "business_id": 1, "status": "needs_review", "decision": "add_with_review"},
        # 同一页面在另一家分店的记录被审查跳过（重复证据）：渲染可见也不重新加入
        {"url": URL, "business_id": 2, "status": "needs_review", "decision": "skip", "reasons": ["duplicate_evidence"]},
        {"url": "https://spa.example/hidden", "business_id": 1, "status": "needs_review", "decision": "add_with_review"},
        {"url": "https://spa.example/offers", "business_id": 1, "status": "valid", "decision": "add_with_review"},
        {"url": "https://spa.example/blocked", "business_id": 1, "status": "needs_review", "decision": "add_with_review"},
    ]
    verdicts = {URL: {"verdict": "verified"}, "https://spa.example/hidden": {"verdict": "not_visible"},
                "https://spa.example/blocked": {"verdict": "page_unavailable", "reason": "net::ERR_BLOCKED_BY_CLIENT"}}
    apply_render_verdicts(decisions, verdicts)
    assert [r["decision"] for r in decisions] == ["add", "skip", "skip", "add", "add_with_review"]
    assert decisions[1]["reasons"] == ["duplicate_evidence"]
    # 渲染没有得到可用页面不是页面不可见：保留等待，下次运行再渲染
    assert decisions[4]["render_review_deep"]["verdict"] == "page_unavailable" and "reasons" not in decisions[4]


def test_text_verdicts_need_ocr_only_for_rendered_pages_whose_text_does_not_show_the_claims():
    items = [{"url": "https://a.com/x", "claims": [{"line": "Botox $10 per unit", "anchors": ["$10"]}]},
             {"url": "https://b.com/x", "claims": [{"line": "Filler $500 per syringe", "anchors": ["$500"]}]},
             {"url": "https://c.com/x", "claims": [{"line": "Peel $99", "anchors": ["$99"]}]}]
    records = {"https://a.com/x": _record("https://a.com/x", ["Botox $10 per unit"]),
               "https://b.com/x": _record("https://b.com/x", ["Book now"], images=["https://b.com/flyer.jpg"]),
               "https://c.com/x": {"url": "https://c.com/x", "error": "page.goto failed: net::ERR_BLOCKED_BY_CLIENT"}}
    verdicts, needs_ocr = text_verdicts(items, records)
    assert verdicts["https://a.com/x"]["verdict"] == "verified"
    assert verdicts["https://c.com/x"] == {"verdict": "page_unavailable",
                                           "reason": "page.goto failed: net::ERR_BLOCKED_BY_CLIENT"}
    assert needs_ocr == ["https://b.com/x"]

    read = []

    def read_image(url):
        read.append(url)
        return {"url": url, "text": "FILLER $500 PER SYRINGE"}

    image_verdicts(verdicts, needs_ocr, items, records, read_image)
    assert read == ["https://b.com/flyer.jpg"]
    assert verdicts["https://b.com/x"]["verdict"] == "verified" and verdicts["https://b.com/x"]["source"] == "image_text"
    assert verdicts["https://b.com/x"]["images_read"] == 1


def test_an_image_visitors_cannot_load_gives_no_text_and_no_negative_finding_about_ocr():
    items = [{"url": "https://b.com/x", "claims": [{"line": "Filler $500 per syringe", "anchors": ["$500"]}]}]
    records = {"https://b.com/x": _record("https://b.com/x", ["Book now"], images=["https://b.com/gone.jpg"])}
    verdicts, needs_ocr = text_verdicts(items, records)
    image_verdicts(verdicts, needs_ocr, items, records, lambda url: {"url": url, "unavailable": "http_404"})
    assert verdicts["https://b.com/x"]["verdict"] == "not_visible" and verdicts["https://b.com/x"]["images_read"] == 0


def test_one_command_renders_every_waiting_page_and_writes_the_decisions_back(tmp_path):
    decisions = [{"url": "https://a.com/x", "business_id": 1, "status": "needs_review", "decision": "add_with_review",
                  "evidence": ["Botox $10 per unit"]},
                 {"url": "https://a.com/x", "business_id": 2, "status": "needs_review", "decision": "add_with_review",
                  "evidence": ["Botox $10 per unit"]},
                 {"url": "https://b.com/x", "business_id": 3, "status": "needs_review", "decision": "skip",
                  "evidence": ["Peel $99"]}]
    decisions_path(tmp_path).write_text(json.dumps({"decisions": decisions}))
    rendered = []

    def ego(shard_path, out_path, timeout_ms):
        shard = json.loads(shard_path.read_text())
        rendered.extend(item["url"] for item in shard["items"])
        out_path.write_text(json.dumps({"results": [_record(i["url"], ["Botox $10 per unit"]) for i in shard["items"]]}))

    summary = review_batch(tmp_path, shards=2, run=ego)
    assert rendered == ["https://a.com/x"]  # 每个网址渲染一次，已跳过的记录不渲染
    assert [d["decision"] for d in json.loads(decisions_path(tmp_path).read_text())["decisions"]] == ["add", "add", "skip"]
    assert json.loads((render_dir(tmp_path) / "verdicts.json").read_text())["https://a.com/x"]["verdict"] == "verified"
    assert summary == {"add": 2, "skip": 1}
