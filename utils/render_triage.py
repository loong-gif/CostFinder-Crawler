"""extract-pages 计划的渲染分流：用代码规则判定每条促销是直接拒绝、需要渲染，还是页面文本已可追溯。

输入是没有可见性清单时生成的计划（条目带 content 与 source_reviews），以及各源页面的最新 scrape。
三种去向：
  reject              内容命中规则缺陷（链接残留、整页列表、价目表 URL、剔除碎片后无内容），不渲染
  render              存在页面文本中找不到的行、来源复核发现漏抽的卡片或没有页面文本，交给 ego-lite 渲染核验
  markdown_traceable  剔除碎片后的每一行都能在页面文本中追溯，且来源复核没有漏抽的卡片
"""
from __future__ import annotations

import re
from typing import Any, Mapping

from utils.page_extraction_snapshot import _page_evidence
from utils.render_review import claim_matches
from utils.source_url import source_url_key

MAX_CONTENT_LINES = 15
LINK_RESIDUE = re.compile(r"\]\(|https?://|!\[|javascript:", re.I)
PRICE_LIST_URL = re.compile(r"price[-_ ]?list", re.I)
WORD = re.compile(r"[A-Za-z]{2,}")
# 只表达价格关系、不含服务或商品名称的词
PRICE_WORDS = frozenset({"off", "per", "unit", "units", "save", "only", "mo", "month", "starting", "from",
                         "regular", "reg", "was", "now", "each", "the", "and"})


def fragment_reason(line: str) -> str | None:
    """返回行是碎片的原因；完整陈述返回 None。"""
    text = line.strip()
    if not WORD.search(text):
        return "no_words"
    if text.startswith("/") and len(text) <= 8:
        return "cut_price_suffix"
    if len(text) < 14 and not any(w.lower() not in PRICE_WORDS for w in WORD.findall(text)):
        return "isolated_figure"
    return None


def triage_entry(entry: Mapping[str, Any], page_text: str) -> dict[str, Any]:
    content = list(entry.get("content") or [])
    junk = {line: reason for line in content if (reason := fragment_reason(line))}
    clean = [line for line in content if line not in junk]
    result: dict[str, Any] = {"source_url": entry["source_url"], "source_pages": entry.get("source_pages") or [entry["source_url"]],
                              "fragment_lines": junk, "clean_content": clean, "reasons": []}
    reasons = result["reasons"]
    if any(LINK_RESIDUE.search(line) for line in content):
        reasons.append("link_residue")
    if len(content) > MAX_CONTENT_LINES:
        reasons.append("oversize_page_list")
    if PRICE_LIST_URL.search(entry["source_url"]):
        reasons.append("price_list_url")
    if not clean:
        reasons.append("no_content_after_fragment_removal")
    if reasons:
        return {**result, "route": "reject"}
    missed = [title for review in entry.get("source_reviews") or [] for title in review.get("source_cards_not_extracted") or []]
    untraceable = [line for line in clean if not page_text or not claim_matches({"line": line, "is_name": False}, [page_text])]
    if missed:
        reasons.append("source_cards_not_extracted")
        result["source_cards_not_extracted"] = missed
    if untraceable:
        reasons.append("line_not_in_page_text" if page_text else "no_page_text")
        result["untraceable_lines"] = untraceable
    return {**result, "route": "render" if reasons else "markdown_traceable"}


def triage_plan(plan: Mapping[str, Any], scrapes: Mapping[str, Mapping[str, Any]]) -> dict[str, Any]:
    entries = []
    for entry in plan["entries"]:
        if entry["kind"] != "promotion" or entry["action"] == "rejected":
            continue
        pages = entry.get("source_pages") or [entry["source_url"]]
        text = "\n\n".join(_page_evidence(scrapes[key]) for url in pages if (key := source_url_key(url)) in scrapes)
        entries.append(triage_entry(entry, text))
    counts: dict[str, int] = {}
    for item in entries:
        counts[item["route"]] = counts.get(item["route"], 0) + 1
    render_pages = sorted({url for item in entries if item["route"] == "render" for url in item["source_pages"]})
    return {"summary": counts, "render_pages": render_pages, "entries": entries}
