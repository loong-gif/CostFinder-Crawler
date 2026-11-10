# 监控自动处理的判定规则（纯函数）：失败分类、基础设施故障、站点失败天数、移出与复查、人工审查存档。
# 站点一侧的失败按不同的 UTC 日期计数，满 14 天移出监控，每 30 天复查一次；
# 其余失败是流水线自身的问题，每日重试，从不移出监控。
from __future__ import annotations

from collections import Counter
from datetime import datetime, timedelta

from utils.monitor_price_writer_plan import normalize_source_url

SITE_REASONS = frozenset({"ego_scrape_failed", "firecrawl_http_error", "firecrawl_empty_markdown",
                          "ego_output_incomplete", "page_not_accessible"})
# AGENTS.md 规定的三类人工审查：Source/Offer 身份、跨域跳到第三方、人工保护
HUMAN_REVIEW = frozenset({"ambiguous_offer_match", "ambiguous_item_match", "ambiguous_source_campaign",
                          "source_business_ambiguous", "source_redirect_requires_review", "protected_row_or_cascade"})
# 凭据被拒是本系统的问题，无论比例都记为基础设施故障
INFRA_REASONS = frozenset({"extraction_credentials_rejected"})
INFRA_SHARE = 0.3
REMOVAL_DAYS = 14
RECHECK_INTERVAL = timedelta(days=30)


def reason_key(reason: str) -> str:
    return reason.split(":", 1)[0].strip()


def classify(reason: str) -> str:
    return "site" if reason_key(reason) in SITE_REASONS else "pipeline"


def capture_outcomes(reports: list[dict]) -> dict[str, dict]:
    outcomes = {}
    for report in reports:
        for url in report.get("successful_urls", []):
            outcomes[normalize_source_url(url)] = {"url": url, "stage": "processing", "reason": None}
        for row in report.get("page_errors", []):
            outcomes[normalize_source_url(row["url"])] = {"url": row["url"], "stage": "scrape",
                                                          "reason": reason_key(row["error"])}
    return outcomes


def processing_reason(row: dict) -> str | None:
    """待重试事件的失败原因，站点一侧的原因优先；其他状态不是失败。"""
    if row["status"] != "retryable":
        return None
    reasons = [reason_key(item["reason"]) for item in row.get("review", []) if item.get("reason")]
    site = [reason for reason in reasons if classify(reason) == "site"]
    return (site or reasons or [None])[0]


def run_outcomes(reports: list[dict], sources: list[dict]) -> dict[str, dict]:
    """本轮抓取的每个页面：{url, stage, reason}；reason 为 None 表示抓取与处理都没有失败。"""
    outcomes = capture_outcomes(reports)
    for row in sources:
        outcome = outcomes.get(normalize_source_url(row["url"]))
        if outcome is not None and outcome["stage"] == "processing":
            outcome["reason"] = processing_reason(row)
    return outcomes


def infra_faults(outcomes: dict[str, dict], health: list[dict], previously_failing: set[str]) -> list[dict]:
    """运行前健康检查失败时整轮都是基础设施故障；否则列出上一次抓取成功的页面中 30% 以上共有的失败原因。

    已在失败的来源（正在计数站点失败天数、暂停小时重试、移出监控后复查）不进入比例：
    它们每天都被重新抓取，计入后会让长期无法访问的站点被永久当作基础设施故障。
    """
    if health:
        return list(health)
    healthy = [outcome for key, outcome in outcomes.items() if key not in previously_failing]
    counts = Counter(outcome["reason"] for outcome in healthy if outcome["reason"])
    faults = {reason: count for reason, count in counts.items() if count >= INFRA_SHARE * len(healthy)}
    faults.update(Counter(outcome["reason"] for outcome in outcomes.values() if outcome["reason"] in INFRA_REASONS))
    return [{"reason": reason, "count": count, "total": len(healthy)} for reason, count in sorted(faults.items())]


def is_infra(reason: str, faults: list[dict]) -> bool:
    return any("service" in fault or fault["reason"] == reason for fault in faults)


def action(at: datetime, outcome: dict, act: str, classification: str, evidence: dict) -> dict:
    return {"time": at.isoformat(), "url": outcome["url"], "stage": outcome["stage"], "reason": outcome["reason"],
            "classification": classification, "action": act, "evidence": evidence}


def site_state(outcome: dict, faults: list[dict]) -> tuple[bool, bool]:
    """返回 (计入站点失败, 站点可以访问)。处理阶段的流水线失败说明页面已抓到：站点可以访问。"""
    reason = outcome["reason"]
    if reason is None:
        return False, True
    site = classify(reason) == "site"
    return site and not is_infra(reason, faults), outcome["stage"] == "processing" and not site


def recheck(removals: dict, key: str, outcome: dict, *, site: bool, reachable: bool, at: datetime) -> list[dict]:
    removed = removals[key]
    if reachable:
        del removals[key]
        return [action(at, outcome, "restored", "site", {"removed_at": removed["removed_at"]})]
    if site and at >= datetime.fromisoformat(removed["next_recheck_at"]):
        removed["next_recheck_at"] = (at + RECHECK_INTERVAL).isoformat()
        return [action(at, outcome, "recheck_failed", "site", {"next_recheck_at": removed["next_recheck_at"]})]
    return []


def count_site_failure(tracking: dict, removals: dict, key: str, outcome: dict, at: datetime) -> list[dict]:
    entry = tracking.setdefault(key, {"url": outcome["url"], "dates": []})
    entry["dates"] = sorted({*entry["dates"], at.date().isoformat()})
    entry["reason"] = outcome["reason"]
    if len(entry["dates"]) < REMOVAL_DAYS:
        return []
    del tracking[key]
    removals[key] = {"url": outcome["url"], "reason": outcome["reason"], "removed_at": at.isoformat(),
                     "next_recheck_at": (at + RECHECK_INTERVAL).isoformat()}
    return [action(at, outcome, "removed", "site", {"dates": entry["dates"]})]


def advance_sources(tracking: dict, removals: dict, outcomes: dict[str, dict], faults: list[dict],
                    at: datetime) -> list[dict]:
    """就地更新站点失败天数（tracking）与移出名单（removals，键为规范化地址），返回本轮动作。"""
    actions = []
    for key, outcome in sorted(outcomes.items()):
        site, reachable = site_state(outcome, faults)
        if key in removals:
            actions += recheck(removals, key, outcome, site=site, reachable=reachable, at=at)
        elif reachable:
            tracking.pop(key, None)
        elif site:
            actions += count_site_failure(tracking, removals, key, outcome, at)
    return actions


def last_human_outcomes(log_rows: list[dict]) -> dict[str, tuple[str, ...]]:
    """存档里每个来源最后一次的人工审查结论；空元组表示已解决。"""
    last = {}
    for row in log_rows:
        if row["action"] == "awaiting_human":
            last[normalize_source_url(row["url"])] = tuple(row["reason"].split(","))
        elif row["action"] == "resolved":
            last[normalize_source_url(row["url"])] = ()
    return last


def review_actions(sources: list[dict], outcomes: dict[str, dict], last: dict[str, tuple[str, ...]],
                   at: datetime) -> list[dict]:
    """本轮重新抓取并处理过的来源：仍属三类人工审查的存档为等待人工决定（结论变化时再存档），不再需要的记为已解决。"""
    actions = []
    for row in sources:
        key = normalize_source_url(row["url"])
        outcome = outcomes.get(key)
        if outcome is None or outcome["stage"] != "processing":
            continue
        reasons = {item.get("reason") for item in row.get("review", [])} if row["status"] == "review" else set()
        human = tuple(sorted(reasons & HUMAN_REVIEW))
        previous = last.get(key, ())
        if human and human != previous:
            actions.append({**action(at, {**outcome, "reason": ",".join(human)}, "awaiting_human", "human_review",
                                     {"plan": row.get("plan_path")}), "stage": "review"})
        elif not human and previous:
            actions.append({**action(at, {**outcome, "reason": ",".join(previous)}, "resolved", "human_review",
                                     {"status": row["status"]}), "stage": "review"})
        last[key] = human
    return actions
