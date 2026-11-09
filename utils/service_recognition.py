"""offer item 的服务识别：别名扫描与确定性规则，规则无法判定时由 LLM 在数据库枚举值内选择。"""
from __future__ import annotations

import json
import re
from functools import lru_cache
from typing import Any, Protocol

from utils.clinic_service_extraction import _normalize_unit_type, infer_service_name_for_item
from utils.clinic_service_taxonomy import _SERVICE_NAME_ENUMS, _normalized_name, service_name_enum_hint
from utils.offer_extraction_llm import _service_dictionary_indexes

LLM_BATCH_SIZE = 25
LLM_NONE = "NONE"
LLM_MULTIPLE = "MULTIPLE"

# 通用服务名被同族具体名称覆盖时，只保留具体名称
SUBSUMED_BY = {
    "Neurotoxin": {"Botox", "Dysport", "Xeomin", "Jeuveau", "Daxxify", "Letybo", "Myobloc"},
    "Dermal Filler": {"Juvéderm", "Restylane", "Radiesse", "Belotero", "RHA Filler", "Skinvive", "Versa",
                      "Evolysse", "EZ Gel", "Sculptra"},
    "Microneedling": {"Morpheus8", "RF Microneedling", "SkinPen Microneedling", "Sylfirm X", "Microchanneling"},
    # Clear + Brilliant 常以「laser facial」宣传
    "Facial": {"Hydrafacial", "DiamondGlow", "Glo2 Facial", "Customized Facial", "Dermaplaning", "Clear + Brilliant"},
    "Chemical Peel": {"VI Peel", "Lunchtime Peel", "PRX Skin Perfecting", "Perfect Derma Peel", "CoolPeel"},
    "Laser Treatment": {"CO2 Laser", "Halo Laser", "Moxi Laser", "Picoway", "Vbeam", "ClearSilk", "Laser Genesis",
                        "Fraxel", "Clear + Brilliant", "Pearl Fractional"},
    "Skin Tightening": {"Ultherapy", "Thermage", "HIFU", "Sofwave", "Tixel", "Forma", "Morpheus8"},
    "Body Contouring": {"CoolSculpting", "Emsculpt", "Lipo Dissolve"},
}

# 服务枚举中没有对应值、名称又与某个枚举相近的项目不交给 LLM 选近似值：
# Emsella 是盆底治疗椅，不是 Emsculpt；腿部静脉治疗没有写明设备时不是 Vbeam
_NOT_IN_ENUM_RE = re.compile(r"\bemsella\b|\bveins?\b", re.IGNORECASE)

UNIT_TYPE_WORDS = {"unit": "unit", "units": "unit", "syringe": "syringe", "syringes": "syringe",
                   "vial": "vial", "vials": "vial", "session": "session", "sessions": "session",
                   "area": "area", "areas": "area", "shot": "shot", "shots": "shot"}


class StructuredClient(Protocol):
    def create_json_response(self, messages: list[dict[str, str]], *, json_schema: dict[str, Any] | None = None
                             ) -> dict[str, Any]: ...


@lru_cache(maxsize=1)
def _scan_keys() -> list[tuple[str, str]]:
    """别名与枚举名（规范化后）到枚举值的映射，按键长度降序。"""
    keys: dict[str, str] = {key: value for key, value in _SERVICE_NAME_ENUMS.items() if key}
    for alias, canonical in _service_dictionary_indexes()["alias_by_normalized"].items():
        hint = service_name_enum_hint(canonical)
        key = _normalized_name(alias)
        if hint is not None and key:
            keys.setdefault(key, hint)
    return sorted(keys.items(), key=lambda pair: len(pair[0]), reverse=True)


@lru_cache(maxsize=1)
def _scan_patterns() -> list[tuple[re.Pattern[str], str]]:
    return [(re.compile(rf"(?<![a-z0-9]){re.escape(key)}(?![a-z0-9])"), value) for key, value in _scan_keys()]


def scan_services(text: Any) -> set[str]:
    """文本中出现的全部枚举服务名；长键优先，已匹配区间不再重复匹配，通用名被同族具体名覆盖。"""
    normalized = _normalized_name(text)
    if not normalized:
        return set()
    consumed = [False] * len(normalized)
    found: set[str] = set()
    for pattern, value in _scan_patterns():
        for match in pattern.finditer(normalized):
            if any(consumed[match.start():match.end()]):
                continue
            for index in range(match.start(), match.end()):
                consumed[index] = True
            found.add(value)
    return {name for name in found if not (SUBSUMED_BY.get(name, set()) & found)}


def item_unit_type(item_name: Any) -> str | None:
    """item_name 中可确认的计量单位；出现多种不同单位时不确认。"""
    words = re.findall(r"[a-z]+", str(item_name or "").casefold())
    found = {UNIT_TYPE_WORDS[word] for word in words if word in UNIT_TYPE_WORDS}
    return _normalize_unit_type(next(iter(found))) if len(found) == 1 else None


def recognize_deterministic(*, item_name: Any, offer_raw_text: str, quantity: Any, sibling_count: int,
                            is_package: bool) -> dict[str, Any]:
    """确定性识别。返回 {service_name, basis, reason}；basis 为 deterministic / needs_llm / unresolved。"""
    if _NOT_IN_ENUM_RE.search(str(item_name or "")):
        return {"service_name": None, "basis": "unresolved", "reason": "service_not_in_enum"}
    in_item = scan_services(item_name)
    if len(in_item) == 1:
        return {"service_name": next(iter(in_item)), "basis": "deterministic", "reason": "item_name"}
    if len(in_item) > 1:
        return {"service_name": None, "basis": "unresolved", "reason": "multiple_services_in_item",
                "candidates": sorted(in_item)}
    if sibling_count > 1 or is_package:
        return {"service_name": None, "basis": "needs_llm", "reason": "no_match_in_multi_item_offer"}
    in_text = scan_services(offer_raw_text)
    if len(in_text) == 1:
        return {"service_name": next(iter(in_text)), "basis": "deterministic", "reason": "offer_raw_text"}
    if len(in_text) > 1:
        return {"service_name": None, "basis": "unresolved", "reason": "multiple_services_in_offer_text",
                "candidates": sorted(in_text)}
    inferred = service_name_enum_hint(infer_service_name_for_item(
        offer_raw_text=offer_raw_text, quantity=quantity, item_name=item_name,
        sibling_count=sibling_count, is_package=is_package))
    if inferred is not None and inferred != "Others":
        return {"service_name": inferred, "basis": "deterministic", "reason": "quantity_heuristic"}
    return {"service_name": None, "basis": "needs_llm", "reason": "no_match"}


class LlmServiceResolver:
    """用 LLM 在数据库枚举值内为单个 item 选择服务；多个服务或无法判定时返回 NONE / MULTIPLE。"""

    def __init__(self, client: StructuredClient, enum_values: list[str]) -> None:
        self.client = client
        self.enum_values = list(enum_values)
        self.schema = {
            "type": "object",
            "additionalProperties": False,
            "required": ["results"],
            "properties": {"results": {"type": "array", "items": {
                "type": "object",
                "additionalProperties": False,
                "required": ["index", "service_name"],
                "properties": {"index": {"type": "integer"},
                               "service_name": {"type": "string", "enum": self.enum_values + [LLM_NONE, LLM_MULTIPLE]}},
            }}},
        }

    def resolve(self, requests: list[dict[str, Any]]) -> dict[int, str]:
        answers: dict[int, str] = {}
        for start in range(0, len(requests), LLM_BATCH_SIZE):
            # 发给 LLM 的 index 是批内序号 0..n-1：稀疏的大 item id 会让模型补出中间不存在的序号
            ids = [request["index"] for request in requests[start:start + LLM_BATCH_SIZE]]
            batch = [{**request, "index": position}
                     for position, request in enumerate(requests[start:start + LLM_BATCH_SIZE])]
            messages = [
                {"role": "system", "content": (
                    "You map medical-aesthetics promotion items to exactly one service_name from the allowed enum. "
                    f"Answer {LLM_MULTIPLE} when the item bundles several different services, and {LLM_NONE} "
                    "when no allowed service is clearly identified. Use only the given text.")},
                {"role": "user", "content": json.dumps(batch, ensure_ascii=False)},
            ]
            payload = self.client.create_json_response(messages, json_schema=self.schema)
            results = payload["results"]
            returned = sorted(entry["index"] for entry in results)
            if returned != sorted(request["index"] for request in batch):
                raise RuntimeError(f"llm_index_mismatch: expected {[r['index'] for r in batch]}, got {returned}")
            for entry in results:
                if entry["service_name"] not in self.enum_values + [LLM_NONE, LLM_MULTIPLE]:
                    raise RuntimeError(f"llm_invalid_service_name: {entry['service_name']}")
                answers[ids[entry["index"]]] = entry["service_name"]
        return answers
