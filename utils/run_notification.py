"""监控运行结束后的飞书通知：汇总本轮 sync_report，以「小扣」机器人私信发给负责人。

凭据来自环境变量 ``FEISHU_APP_ID``、``FEISHU_APP_SECRET``，收件人为 ``FEISHU_NOTIFY_OPEN_ID``。
"""
from __future__ import annotations

import json
import os
from datetime import datetime
from typing import Any, Mapping, Sequence
from zoneinfo import ZoneInfo

import requests

FEISHU_API = "https://open.feishu.cn/open-apis"
RUN_NAMES = {"full_monitor": "每日监控", "failure_retry": "失败重试", "event_processing": "事件处理"}
MAX_LISTED = 5
PACIFIC = ZoneInfo("America/Los_Angeles")


def _local(value: str) -> str:
    return datetime.fromisoformat(value.replace("Z", "+00:00")).astimezone(PACIFIC).strftime("%Y-%m-%d %H:%M PT")


def run_has_work(report: Mapping[str, Any]) -> bool:
    """小时重试没有抓取页面、也没有处理事件时不通知。"""
    return bool(sum(row.get("total_pages", 0) for row in report.get("monitors", [])) or report.get("selected_events"))


def report_message(report: Mapping[str, Any], *, started_at: str, exit_code: int,
                   tiers: Mapping[str, Any] | None) -> str:
    monitors = report.get("monitors", [])
    pages = sum(row.get("total_pages", 0) for row in monitors)
    failed = sum(row.get("error", 0) for row in monitors)
    changed = sum(row.get("changed", 0) for row in monitors)
    minutes = (datetime.fromisoformat(report["generated_at"]) - datetime.fromisoformat(started_at)).total_seconds() / 60
    counts = report.get("source_status_counts", {})
    lines = [f"【CostFinder {RUN_NAMES.get(report.get('run_type'), report.get('run_type'))}】{_local(started_at)}，"
             f"用时 {minutes:.0f} 分钟，退出码 {exit_code}",
             f"抓取：{pages} 页，成功 {pages - failed}，失败 {failed}，价格变化 {changed}"]
    if tiers is not None:
        by_tier = tiers["by_tier_days"]
        lines.append(f"轮询档期：7 天 {by_tier['7']} 页 / 15 天 {by_tier['15']} 页 / 30 天 {by_tier['30']} 页；"
                     f"会员页 60 天 {by_tier.get('60', 0)} 页 / 90 天 {by_tier.get('90', 0)} 页，"
                     f"未建档 {tiers['untracked']} 页")
    status_text = "，".join(f"{status} {count}" for status, count in sorted(counts.items())) or "无"
    lines.append(f"事件：处理 {report.get('selected_events', 0)} 个（{status_text}）；"
                 f"到期活动核验 {report.get('expiry_checks_enqueued', 0)}，数据质量复查 {report.get('quality_checks_enqueued', 0)}")
    pending = [row for row in report.get("sources", []) if row.get("status") == "pending_sql"]
    lines.append(f"待执行 SQL {len(pending)} 个"
                 + ("，需经 Supabase MCP 执行" if pending else ""))
    for row in pending[:MAX_LISTED]:
        lines.append(f"  · {row['url']}")
    paused = report.get("paused_retries", [])
    if paused:
        lines.append(f"暂停小时重试：{len(paused)} 个来源（每日任务继续重试）")
    errors = [error for row in monitors for error in row.get("page_errors", [])]
    if errors:
        lines.append(f"抓取失败页面（列出 {min(len(errors), MAX_LISTED)}/{len(errors)}）：")
        lines.extend(f"  · {error['url']} — {error['error']}" for error in errors[:MAX_LISTED])
    return "\n".join(lines)


def crash_message(*, run_type: str, started_at: str, exit_code: int, log_tail: Sequence[str]) -> str:
    lines = [f"【CostFinder {RUN_NAMES.get(run_type, run_type)}】{_local(started_at)} 运行异常终止，退出码 {exit_code}，"
             "本轮没有生成运行报告。", "日志末尾："]
    return "\n".join(lines + list(log_tail))


def send_text(text: str) -> str:
    token = requests.post(f"{FEISHU_API}/auth/v3/tenant_access_token/internal", timeout=30,
                          json={"app_id": os.environ["FEISHU_APP_ID"],
                                "app_secret": os.environ["FEISHU_APP_SECRET"]})
    token.raise_for_status()
    body = token.json()
    if body["code"] != 0:
        raise RuntimeError(f"feishu_token_failed: {body['code']} {body.get('msg')}")
    sent = requests.post(f"{FEISHU_API}/im/v1/messages", params={"receive_id_type": "open_id"}, timeout=30,
                         headers={"Authorization": f"Bearer {body['tenant_access_token']}"},
                         json={"receive_id": os.environ["FEISHU_NOTIFY_OPEN_ID"], "msg_type": "text",
                               "content": json.dumps({"text": text}, ensure_ascii=False)})
    sent.raise_for_status()
    result = sent.json()
    if result["code"] != 0:
        raise RuntimeError(f"feishu_send_failed: {result['code']} {result.get('msg')}")
    return result["data"]["message_id"]
