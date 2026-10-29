"""extract-pages 的可见性清单：计划中每页抽出的内容行交给 ego-lite 深度渲染核验，按渲染后可见文本逐行判定。

清单格式即 rendered_visibility_gate 读取的 {url: {status, claimResults: [{recordType, claim, status}]}}。
"""
from __future__ import annotations

from typing import Any, Mapping

from utils.render_review import claim_anchors, claim_matches, corpus_text, item_id


def _page_claims(plan: Mapping[str, Any]) -> dict[str, list[tuple[str, str]]]:
    """每个源页面的 (recordType, 内容行)；促销取 content，会员取名称与权益。"""
    pages: dict[str, list[tuple[str, str]]] = {}
    for entry in plan["entries"]:
        if entry["kind"] == "promotion" and entry.get("content"):
            lines = [("promotion", line) for line in entry["content"]]
        elif entry["kind"] == "membership":
            lines = [("membership", entry["membership_name"])] + \
                    [("membership", line) for line in entry.get("benefits") or []]
        else:
            continue
        for page in entry.get("source_pages") or [entry["source_url"]]:
            known = pages.setdefault(page, [])
            known.extend(line for line in lines if line not in known)
    return pages


def build_shard(plan: Mapping[str, Any], name: str) -> dict[str, Any]:
    items = []
    for url, lines in sorted(_page_claims(plan).items()):
        claims = [{"id": i, "record_type": kind, "line": line[:240], "anchors": claim_anchors(line)}
                  for i, (kind, line) in enumerate(lines)]
        items.append({"id": item_id(url), "url": url, "claims": claims})
    return {"name": name, "items": items}


def build_manifest(shard: Mapping[str, Any], render_out: Mapping[str, Any]) -> dict[str, Any]:
    records = {r["url"]: r for r in render_out["results"]}
    manifest: dict[str, Any] = {}
    for item in shard["items"]:
        record = records.get(item["url"])
        if record is None or record.get("error"):
            manifest[item["url"]] = {"status": "page_not_accessible",
                                     "reason": (record or {}).get("error", "not_rendered"), "claimResults": []}
            continue
        corpus = corpus_text(record["result"]["texts"])
        manifest[item["url"]] = {
            "status": "page_accessible",
            "checked_at": record.get("checked_at"),
            "claimResults": [{"recordType": claim["record_type"], "claim": claim["line"],
                              "status": "visible_match" if claim_matches(claim, corpus) else "not_visible"}
                             for claim in item["claims"]],
        }
    return manifest
