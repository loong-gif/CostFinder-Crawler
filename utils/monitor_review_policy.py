"""监控计划到事件状态：就绪的计划进入写入，临时故障等待重试，会员页排除，其余进入审查。"""
from __future__ import annotations

TRANSIENT = {"rendered_page_failed", "page_not_accessible", "rendered_page_timeout", "rendered_browser_unavailable",
             "TimeoutExpired", "TimeoutError", "HTTPError", "ConnectionError",
             "extraction_credentials_missing", "extraction_rate_limited", "extraction_quota_exhausted", "JSONDecodeError",
             "fresh_scrape_required", "image_stage_timeout", "image_tiles_missing", "image_capture_incomplete"}


def event_status(plan: dict) -> str:
    reason = (plan.get("review") or [{}])[-1].get("reason")
    if plan["status"] == "ready":
        return "planned"
    if reason in TRANSIENT:
        return "retryable"
    # 会员页只进 clinic_memberships，促销监控不处理，也不进入审查清单。
    return "excluded" if reason == "membership_source_requires_review" else "review"
