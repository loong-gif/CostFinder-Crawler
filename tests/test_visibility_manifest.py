from __future__ import annotations

from utils.visibility_manifest import build_manifest, build_shard

PLAN = {"entries": [
    {"kind": "promotion", "action": "insert", "source_url": "https://a.com/specials",
     "source_pages": ["https://a.com/specials"], "content": ["Botox $10/unit this month", "Free consultation for Radiesse"]},
    {"kind": "promotion", "action": "update", "source_url": "https://b.com/specials", "content": ["Lip filler $650"]},
    {"kind": "promotion", "action": "insert", "source_url": "https://c.com/specials", "content": []},
    {"kind": "promotion", "action": "rejected", "source_url": "https://d.com/specials", "content": ["Botox $9/unit"],
     "reason": "price_page_without_discount"},
    {"kind": "membership", "action": "rejected", "source_url": "https://e.com/membership",
     "reason": "source_page_not_membership"},
]}


def test_shard_has_one_item_per_page_with_content():
    shard = build_shard(PLAN, "run")
    assert [i["url"] for i in shard["items"]] == ["https://a.com/specials", "https://b.com/specials"]
    assert [c["line"] for c in shard["items"][0]["claims"]] == ["Botox $10/unit this month",
                                                                 "Free consultation for Radiesse"]


def test_manifest_marks_visible_lines_and_unrendered_pages():
    shard = build_shard(PLAN, "run")
    render = {"results": [{"url": "https://a.com/specials", "checked_at": "t",
                           "result": {"texts": ["Specials", "Botox $10/unit this month"]}},
                          {"url": "https://b.com/specials", "error": "timeout"}]}
    manifest = build_manifest(shard, render)
    statuses = [c["status"] for c in manifest["https://a.com/specials"]["claimResults"]]
    assert statuses == ["visible_match", "not_visible"]
    assert manifest["https://b.com/specials"] == {"status": "page_not_accessible", "reason": "timeout",
                                                  "claimResults": []}
