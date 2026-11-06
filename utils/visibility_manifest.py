"""extract-pages 的可见性清单：计划中每页抽出的内容行交给 ego-lite 深度渲染核验，按渲染后可见文本逐行判定。

清单格式即 rendered_visibility_gate 读取的 {url: {status, claimResults: [{recordType, claim, status}]}}。
"""
from __future__ import annotations

import subprocess
from pathlib import Path
from typing import Any, Mapping

import requests

from utils.paddle_ocr_markdown import filter_promo_image_urls
from utils.browser_scrape import is_block_page
from utils.render_review import claim_anchors, claim_matches, corpus_text, item_id, ocr_image
from utils.rendered_visibility_gate import IMAGE_OCR_PROOF, image_claim_verified

MIN_RENDERED_CHARS = 300


def membership_fee_claim(name: str, price: Any) -> str:
    """会员费用声明：方案名称加费用，按名称规则核验（名称相邻出现，费用在名称附近）。"""
    return f"{name} ${float(price):g}"


def _page_claims(plan: Mapping[str, Any]) -> dict[str, list[tuple[str, str, bool]]]:
    """每个源页面的 (recordType, 内容行, 是否方案名称)；促销取 content，会员取名称、名称加费用与权益。
    被拒绝的条目不写入，不核验。"""
    pages: dict[str, list[tuple[str, str, bool]]] = {}
    for entry in plan["entries"]:
        if entry["action"] == "rejected":
            continue
        if entry["kind"] == "promotion" and entry.get("content"):
            lines = [("promotion", line, False) for line in entry["content"]]
        elif entry["kind"] == "membership":
            name = entry["membership_name"]
            fee = [] if entry.get("membership_price") is None else [
                ("membership", membership_fee_claim(name, entry["membership_price"]), True)]
            lines = [("membership", name, True)] + fee + \
                    [("membership", line, line.startswith(name)) for line in entry.get("source_claims") or []] + \
                    [("membership", line, False) for line in entry.get("benefits") or []]
        else:
            continue
        for page in entry.get("source_pages") or [entry["source_url"]]:
            known = pages.setdefault(page, [])
            known.extend(line for line in lines if line not in known)
    return pages


def build_shard(plan: Mapping[str, Any], name: str) -> dict[str, Any]:
    items = []
    for url, lines in sorted(_page_claims(plan).items()):
        claims = [{"id": i, "record_type": kind, "line": line, "anchors": claim_anchors(line), "is_name": is_name}
                  for i, (kind, line, is_name) in enumerate(lines)]
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
        texts = record["result"]["texts"]
        if sum(len(t) for t in texts) < MIN_RENDERED_CHARS or is_block_page("\n".join(texts)):
            # 空白页或拦截页说明页面没有正常渲染，声明无从判定
            manifest[item["url"]] = {"status": "page_not_accessible", "reason": "blank_or_blocked_render",
                                     "claimResults": []}
            continue
        image_dir = Path(__file__).resolve().parents[1] / "state" / "visibility_ocr_images"
        image_dir.mkdir(parents=True, exist_ok=True)
        images = filter_promo_image_urls([str(image.get("url") or "") for image in
                                          record["result"].get("images") or [] if isinstance(image, Mapping)])
        image_evidence = []
        for url in images:
            try:
                image_evidence.append({"url": url, "text": ocr_image(url, image_dir)})
            except (requests.RequestException, subprocess.CalledProcessError, FileNotFoundError) as exc:
                image_evidence.append({"url": url, "error": str(exc)})
        claim_results = []
        for claim in item["claims"]:
            status = "visible_match" if claim_matches(claim, texts) else "not_visible"
            visible_evidence = None
            matching_images = [image for image in image_evidence
                               if image.get("text") and claim_matches(claim, [image["text"]])]
            if status == "not_visible" and matching_images:
                status = "image_only"
                visible_evidence = {"kind": IMAGE_OCR_PROOF, "pageUrl": item["url"],
                                    "captured_at": record.get("checked_at"),
                                    "transcript": corpus_text([image["text"] for image in matching_images]),
                                    "imageUrls": [image["url"] for image in matching_images],
                                    "ocrEvidence": matching_images}
            result = {"recordType": claim["record_type"], "claim": claim["line"], "status": status}
            if visible_evidence is not None:
                result["visibleEvidence"] = visible_evidence
            claim_results.append(result)
        page_manifest = {
            "status": "page_accessible",
            "url": item["url"],
            "checked_at": record.get("checked_at"),
            "visibleImages": images,
            "imageOcr": image_evidence,
            "complete": False,
            "claimResults": claim_results,
        }
        page_manifest["complete"] = bool(claim_results) and all(
            claim["status"] == "visible_match" or
            (claim["status"] == "image_only" and image_claim_verified(claim["claim"], claim, page_manifest))
            for claim in claim_results)
        manifest[item["url"]] = page_manifest
    return manifest
