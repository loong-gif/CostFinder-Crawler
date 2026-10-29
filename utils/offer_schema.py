"""offer 抽取 schema：service_name 枚举由调用方传入（来自快照的 clinic_service_name_enum）并注入。"""
from __future__ import annotations

import copy
import json
from pathlib import Path
from typing import Any

SCHEMA_PATH = Path(__file__).resolve().parents[1] / "schema" / "offer_extraction_schema.json"
SERVICE_ENUM_TYPE = "clinic_service_name_enum"


def service_name_enum(rows: list[dict[str, Any]]) -> list[str]:
    """快照 service_enum 表（utils/pipeline_db.SERVICE_NAME_ENUM_SNAPSHOT）的行 → 按 enumsortorder 排序的枚举值。"""
    values = [row["enumlabel"] for row in sorted(rows, key=lambda row: row["enumsortorder"])]
    if not values:
        raise RuntimeError(f"empty_enum:{SERVICE_ENUM_TYPE}")
    return values


def base_offer_schema() -> dict[str, Any]:
    return json.loads(SCHEMA_PATH.read_text(encoding="utf-8"))


def with_service_enum(schema: dict[str, Any], service_names: list[str]) -> dict[str, Any]:
    """返回 schema 副本，items.service_name 的枚举为给定服务名加 null。"""
    if not service_names:
        raise ValueError("service_names must not be empty")
    result = copy.deepcopy(schema)
    item = result["properties"]["offers"]["items"]["properties"]["items"]["items"]
    item["properties"]["service_name"]["enum"] = list(dict.fromkeys(service_names)) + [None]
    return result


def offer_schema(service_names: list[str]) -> dict[str, Any]:
    return with_service_enum(base_offer_schema(), service_names)
