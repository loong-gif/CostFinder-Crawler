"""
站内价格/促销页发现爬虫
"""
from __future__ import annotations

from typing import Optional


def normalize_process_flag(value: Optional[str]) -> str:
    return (value or "").strip().lower()


def is_filtered_process_flag(value: Optional[str]) -> bool:
    return normalize_process_flag(value) == "filtered"
