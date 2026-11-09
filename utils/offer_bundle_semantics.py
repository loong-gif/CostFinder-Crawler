"""组合 offer 的语义：「+」连接的组合项目拆成 item，按原文判定是否为套餐，分号分隔的两条 offer 拆开。"""
from __future__ import annotations

from copy import deepcopy
import re
from typing import Any, Mapping

from utils.service_price_guard import infer_explicit_per_unit_price

_BUY_GET = re.compile(r"\b(?:buy|purchase)\b.*\b(?:get|receive)\b|\band/or\b", re.I)
# 连接两个项目的「+」；数字后的「+」（8+ Treatments、3+ syringes）表示「及以上」，不是连接符
JOIN_PLUS = r"(?<!\d)\s*\+\s*"


def _item_label(item: Mapping[str, Any]) -> str:
    return str(item.get("item_name") or item.get("service_name") or "").strip()


def _parse_quantity_prefix(name: str) -> tuple[str, int | None, str | None]:
    m = re.match(
        r"^(\d+)\s*(units?|sessions?|syringes?|treatments?)\s+(?:of\s+)?(.+)$",
        name.strip(),
        re.I,
    )
    if m:
        unit_type = "unit" if m.group(2).casefold().startswith("unit") else m.group(2).casefold().rstrip("s")
        return m.group(3).strip(), int(m.group(1)), unit_type
    m = re.match(r"^(\d+)\s+(.+)$", name.strip())
    if m:
        return m.group(2).strip(), int(m.group(1)), None
    return name.strip(), None, None


def _split_bundle_component_text(name: str) -> list[str]:
    chunks = re.split(JOIN_PLUS, name)
    out: list[str] = []
    for chunk in chunks:
        chunk = re.split(r"\s+\$\s*\d", chunk, maxsplit=1)[0].strip(" ,:;—–-")
        if not chunk:
            continue
        if "," in chunk and re.search(r"\d+\s+\w", chunk):
            out.extend(part.strip() for part in re.split(r",(?!\d{3}\b)", chunk) if part.strip())
        else:
            out.append(chunk)
    return out


def _plus_in_service_name(item: dict, name: str) -> bool:
    """服务名本身含「+」（例如 Clear + Brilliant）且完整出现在 item 名称中：这是一个服务，不是组合。"""
    service = " ".join(str(item.get("service_name") or "").split()).casefold()
    return "+" in service and service in " ".join(name.split()).casefold()


def _expand_bundle_items(items: list[dict], raw: str) -> list[dict]:
    expanded: list[dict] = []
    for item in items:
        name = _item_label(item)
        if _plus_in_service_name(item, name):
            expanded.append(item)
            continue
        parts = _split_bundle_component_text(name) if re.search(JOIN_PLUS + "|&", name) else [name]
        if len(parts) == 1 and len(items) == 1 and re.search(JOIN_PLUS + "|&", raw):
            head = re.split(r"\s*—\s*|\s*-\s*\$", raw, maxsplit=1)[0]
            parts = _split_bundle_component_text(head) or parts
        for part in parts:
            clean, qty, unit_type = _parse_quantity_prefix(part)
            row = deepcopy(item)
            row["item_name"] = clean
            if row.get("service_name") in (None, "", name):
                row["service_name"] = clean
            if qty is not None and row.get("quantity") is None:
                row["quantity"] = qty
            if unit_type and not row.get("unit_type"):
                row["unit_type"] = unit_type
            row["unit_price"] = None
            expanded.append(row)
    return expanded or items


def _should_treat_as_bundle(raw: str, offer: dict[str, Any], items: list[dict]) -> bool:
    if offer.get("purchase_type") == "bundle" or offer.get("is_package") is True:
        return True
    if re.search(r"\b(?:package|bundle|combo)\b", raw, re.I) and not re.search(r"\bor\b|或", raw, re.I):
        return True
    if len(items) < 2:
        return False
    if infer_explicit_per_unit_price(raw) is not None or offer.get("price_model") == "per_unit":
        return False
    if offer.get("discount_price") is None and offer.get("discount_percent") is None:
        return False
    if len(items) > 1 and re.search(JOIN_PLUS + r"|,\s*\d+\s", raw):
        return True
    return False


def _split_semicolon_masters(offer: dict[str, Any]) -> list[dict] | None:
    """When one master quoted two independent promo lines, split before OR/AND rules."""
    raw = str(offer.get("offer_raw_text") or "")
    items = offer.get("items") or []
    if ";" not in raw or len(items) < 2:
        return None
    segments = [segment.strip() for segment in raw.split(";") if segment.strip()]
    if len(segments) != len(items):
        return None
    return [
        {**offer, "offer_raw_text": segment, "purchase_type": "single", "is_package": False, "items": [deepcopy(item)]}
        for segment, item in zip(segments, items)
    ]


def _apply_bundle_semantics(offer: dict[str, Any], raw: str) -> dict[str, Any]:
    if _BUY_GET.search(raw) and len(offer.get("items") or []) > 1:
        tagged = deepcopy(offer)
        tagged.update(purchase_type="bundle", is_package=True)
        return tagged
    items = _expand_bundle_items(offer.get("items") or [], raw)
    if not _should_treat_as_bundle(raw, offer, items):
        return offer
    offer = deepcopy(offer)
    offer.update(purchase_type="bundle", is_package=True, price_model=offer.get("price_model") or "total")
    normalized_items = []
    for item in items:
        row = dict(item)
        row["unit_price"] = None
        normalized_items.append(row)
    offer["items"] = normalized_items
    return offer
