# 监控自动处理的状态文件：移出监控名单、站点失败天数与只追加的处理存档。监控运行是唯一的写入方。
from __future__ import annotations

import json
import os
from datetime import datetime
from pathlib import Path

from utils.monitor_price_store import atomic_json, read_json
from utils.monitor_price_writer_plan import normalize_source_url
from utils.monitor_remediation_policy import (
    advance_sources, infra_faults, last_human_outcomes, review_actions, run_outcomes,
)

REMOVALS = "monitor_removals.json"
SITE_FAILURES = "site_failure_days.json"
LOG = "remediation_log.jsonl"


def load_removals(root: Path) -> dict[str, dict]:
    entries = read_json(root / REMOVALS, {"entries": []})["entries"]
    return {normalize_source_url(entry["url"]): entry for entry in entries}


def removed_until_recheck(root: Path, at: datetime) -> set[str]:
    """移出监控且未到复查时间的来源（规范化地址）。"""
    return {key for key, entry in load_removals(root).items() if datetime.fromisoformat(entry["next_recheck_at"]) > at}


def rechecks_due(root: Path, at: datetime) -> set[str]:
    """移出监控且已到复查时间的来源（规范化地址）。"""
    return set(load_removals(root)) - removed_until_recheck(root, at)


def read_log(root: Path) -> list[dict]:
    path = root / LOG
    return [json.loads(line) for line in path.read_text().splitlines()] if path.exists() else []


def append_log(root: Path, rows: list[dict]) -> None:
    if not rows:
        return
    root.mkdir(parents=True, exist_ok=True)
    with (root / LOG).open("a", encoding="utf-8") as stream:
        stream.write("".join(json.dumps(row, ensure_ascii=False) + "\n" for row in rows))
        stream.flush()
        os.fsync(stream.fileno())


def remediate(root: Path, *, reports: list[dict], sources: list[dict], health: list[dict], at: datetime,
              paused: set[str]) -> dict:
    """按本轮抓取与处理结果更新站点失败天数、移出名单和人工审查存档，返回本轮处理汇总（写入运行报告）。

    paused 是本轮抓取前已暂停小时重试的来源。
    """
    outcomes = run_outcomes(reports, sources)
    tracking = read_json(root / SITE_FAILURES, {"sources": {}})["sources"]
    removals = load_removals(root)
    previously_failing = {*tracking, *removals, *(normalize_source_url(url) for url in paused)}
    faults = infra_faults(outcomes, health, previously_failing)
    actions = [{"time": at.isoformat(), "url": None, "stage": "run", "reason": fault.get("reason", fault.get("service")),
                "classification": "infra", "action": "infra_fault", "evidence": fault} for fault in faults]
    actions += advance_sources(tracking, removals, outcomes, faults, at)
    actions += review_actions(sources, outcomes, last_human_outcomes(read_log(root)), at)
    atomic_json(root / SITE_FAILURES, {"schema_version": 1, "sources": tracking})
    atomic_json(root / REMOVALS, {"schema_version": 1, "entries": sorted(removals.values(), key=lambda e: e["url"])})
    append_log(root, actions)

    def urls(name):
        return [row["url"] for row in actions if row["action"] == name]
    return {"removed": urls("removed"), "restored": urls("restored"), "recheck_failed": urls("recheck_failed"),
            "awaiting_human": [{"url": row["url"], "reasons": row["reason"].split(",")}
                               for row in actions if row["action"] == "awaiting_human"],
            "resolved": urls("resolved"), "infra_faults": faults,
            "site_failures": sorted(({"url": entry["url"], "days": len(entry["dates"]), "reason": entry["reason"]}
                                     for entry in tracking.values()), key=lambda row: (-row["days"], row["url"]))}
