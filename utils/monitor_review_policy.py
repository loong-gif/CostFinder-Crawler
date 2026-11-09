"""监控计划到事件状态。

只有证据无法判定的情形进入人工审查：用户手工保护、无法唯一对应的 Source/Offer 身份、跨域跳到第三方网站、
凭据被拒绝与用户显式关闭的图片写入。临时故障等待重试；其余未确认的情形（页面被拦截或为空、模型输出未通过
校验、截图或渲染证据不全）不改动现有记录，重新抓取后再处理。
"""
from __future__ import annotations

import http.client
import ssl

import requests
import urllib3

REVIEW = {"protected_row_or_cascade", "ambiguous_offer_match", "ambiguous_item_match", "ambiguous_source_campaign",
          "source_business_ambiguous", "source_redirect_requires_review", "extraction_credentials_rejected",
          "image_writes_disabled"}
EXCLUDED = {"membership_source_requires_review", "source_business_missing"}
TRANSIENT = {"rendered_page_failed", "page_not_accessible", "rendered_page_timeout", "rendered_browser_unavailable",
             "TimeoutExpired", "TimeoutError", "HTTPError", "ConnectionError",
             "extraction_credentials_missing", "extraction_rate_limited", "extraction_quota_exhausted", "JSONDecodeError",
             "fresh_scrape_required", "image_stage_timeout", "image_tiles_missing", "image_capture_incomplete"}
# 写入门禁拦下的证据问题：重新抓取渲染即可
RECAPTURE_GATES = {"evidence_expired", "breakpoint_coverage_incomplete", "legacy_image_evidence_requires_refresh",
                   "human_capture_proof_mismatch", "image_capture_expired_requires_refresh",
                   "variant_capture_proof_mismatch", "image_dual_read_required", "image_capture_proof_mismatch",
                   "image_obscured", "image_parent_facts_missing", "image_coverage_incomplete", "image_region_missing",
                   "image_fact_proof_mismatch",
                   # 旧代码生成、仍写 is_offer_extracted 的计划：重新抓取后由现行代码重新生成
                   "derived_is_offer_extracted: regenerate plan"}
NETWORK_ERRORS = (requests.RequestException, ssl.SSLError, ConnectionError, TimeoutError,
                  http.client.HTTPException, urllib3.exceptions.HTTPError)


def needs_review(item: dict) -> bool:
    return item.get("reason") in REVIEW


def event_status(plan: dict) -> tuple[str, bool]:
    """返回 (事件状态, 是否需要重新抓取)。"""
    if plan["status"] == "ready":
        return "planned", False
    items = plan.get("review") or [{}]
    reasons = {item.get("reason") for item in items}
    if any(needs_review(item) for item in items):
        return "review", False
    if reasons & EXCLUDED:
        return "excluded", False
    if all(item.get("reason") in TRANSIENT or item.get("transient") for item in items):
        return "retryable", False
    # 计划内的证据问题带 blocking 标记；其余是代码未预料的异常，交人工
    if all("blocking" in item for item in items):
        return "retryable", True
    return "review", False


def gate_status(category: str) -> tuple[str, bool]:
    """待执行 SQL 渲染被门禁拦下时的事件状态。"""
    return ("retryable", True) if category in RECAPTURE_GATES else ("review", False)
