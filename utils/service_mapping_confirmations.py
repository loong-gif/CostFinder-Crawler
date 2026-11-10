"""link-services 重新判定后确认保留的服务映射，按"offer 原文 + item 名称"记录在 service_mapping_confirmations.json。

quality 的 service_scope_evidence 规则读取同一文件：原文与 item 名称都未变化的已确认映射不再交给 link-services。
确认只对记录时的文字成立，不进入全局别名词典（"face"、"chin" 之类的措辞在别的 offer 里指向别的服务）。
文件随仓库部署到监控主机。
"""
from __future__ import annotations

import json
from collections import defaultdict
from pathlib import Path
from typing import Any, Mapping

from utils.offer_purchase_semantics import _text

CONFIRMATIONS_PATH = Path(__file__).resolve().parents[1] / "service_mapping_confirmations.json"


def confirmation_key(offer_raw_text: Any, item_name: Any) -> tuple[str, str]:
    return _text(offer_raw_text), _text(item_name)


def load_confirmations() -> dict[tuple[str, str], str]:
    """(规范化原文, 规范化 item 名称) → 已确认的服务名。"""
    payload = json.loads(CONFIRMATIONS_PATH.read_text(encoding="utf-8"))
    return {confirmation_key(row["offer_raw_text"], row["item_name"]): row["service_name"]
            for row in payload["confirmations"]}


def confirmed_mappings(entries: list[dict[str, Any]], items: Mapping[int, dict[str, Any]],
                       offers: Mapping[int, dict[str, Any]]) -> list[dict[str, Any]]:
    """LLM 判定与现有映射一致（keep）的 item；同一原文与名称在本计划中被判给不同服务时不记录。
    确定性规则得出的保留不需要记录：quality 直接调用同一规则。"""
    names: dict[tuple[str, str], set[str]] = defaultdict(set)
    kept: dict[tuple[str, str], dict[str, Any]] = {}
    for entry in entries:
        if entry["basis"] != "llm":
            continue
        item = items[entry["offer_item_id"]]
        raw = offers[item["offer_id"]]["offer_raw_text"]
        key = confirmation_key(raw, item["item_name"])
        names[key].add(entry["recognized"])
        if entry["action"] == "keep":
            row = kept.setdefault(key, {"offer_raw_text": raw, "item_name": item["item_name"],
                                        "service_name": entry["recognized"], "offer_item_ids": []})
            row["offer_item_ids"].append(entry["offer_item_id"])
    return [row for key, row in sorted(kept.items()) if len(names[key]) == 1]


def record_confirmations(rows: list[dict[str, Any]]) -> None:
    """写入确认文件：同一原文与名称只保留最新的一条，按原文排序。"""
    payload = json.loads(CONFIRMATIONS_PATH.read_text(encoding="utf-8"))
    merged = {confirmation_key(r["offer_raw_text"], r["item_name"]): r for r in payload["confirmations"]}
    for row in rows:
        merged[confirmation_key(row["offer_raw_text"], row["item_name"])] = {
            "offer_raw_text": row["offer_raw_text"], "item_name": row["item_name"],
            "service_name": row["service_name"]}
    payload["confirmations"] = [merged[key] for key in sorted(merged)]
    CONFIRMATIONS_PATH.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
