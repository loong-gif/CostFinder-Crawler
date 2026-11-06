from __future__ import annotations

import re
from typing import Any

from utils.offer_stage_extract import _fingerprint, candidates_from_payload
from utils.offer_stage_spec import NUMERIC_COLUMNS, PATCH_COLUMNS, SOURCE_SPEC, content_hash


def _number(value: Any) -> float | None:
    return None if value is None else float(value)


def _same(column: str, left: Any, right: Any) -> bool:
    if column in NUMERIC_COLUMNS:
        left, right = _number(left), _number(right)
        return left == right if left is None or right is None else abs(left - right) <= 0.005
    if column == "conditions":
        return list(left or []) == list(right or [])
    if column in {"is_package", "is_membership_required", "is_new_customer_required"}:
        return bool(left) == bool(right)
    return left == right


def _item_same(field: str, left: Any, right: Any) -> bool:
    if field in {"delivered_units", "unit_price"}:
        left, right = _number(left), _number(right)
        return left == right if left is None or right is None else abs(left - right) <= 0.00005
    return left == right


def _name(value: Any) -> str:
    return re.sub(r"\s+", " ", str(value or "")).strip().casefold()


def reconcile_items(existing: list[dict[str, Any]], wanted: list[dict[str, Any]], *, blanket: bool,
                    valid_service_ids: set[int]) -> list[dict[str, Any]]:
    """保留唯一匹配的已有 Item ID 与有效 service_id；blanket 的 service_id 一律为 NULL。"""
    actions: list[dict[str, Any]] = []
    free = list(existing)
    pairs: list[tuple[dict[str, Any], dict[str, Any] | None]] = []
    for item in wanted:
        matches = [old for old in free if _name(old["item_name"]) == _name(item["item_name"])]
        old = matches[0] if len(matches) == 1 else None
        if old is not None:
            free.remove(old)
        pairs.append((item, old))
    unmatched = [pair for pair in pairs if pair[1] is None]
    if len(unmatched) == 1 and len(free) == 1:
        pairs = [(item, free[0]) if old is None else (item, old) for item, old in pairs]
        free = []
    for item, old in pairs:
        if old is None:
            actions.append({"action": "insert", "item_name": item["item_name"], "service_id": None,
                            "delivered_units": item["delivered_units"], "unit_price": item["unit_price"],
                            "service_name_hint": item.get("service_name_hint")})
            continue
        service_id = old["service_id"]
        if blanket or service_id not in valid_service_ids:
            service_id = None
        keep_name = old["service_id"] is not None and service_id is not None
        values = {"item_name": old["item_name"] if keep_name else item["item_name"], "service_id": service_id,
                  "delivered_units": item["delivered_units"], "unit_price": item["unit_price"]}
        current = {"item_name": old["item_name"], "service_id": old["service_id"],
                   "delivered_units": old["delivered_units"], "unit_price": old["unit_price"]}
        changed = {key: value for key, value in values.items() if not _item_same(key, current[key], value)}
        actions.append({"action": "update" if changed else "keep", "offer_item_id": old["offer_item_id"],
                        **values, "changed": changed, "service_name_hint": item.get("service_name_hint"),
                        "service_id_cleared": old["service_id"] is not None and service_id is None and not blanket})
    for old in free:
        actions.append({"action": "delete", "offer_item_id": old["offer_item_id"], "item_name": old["item_name"],
                        "service_id": old["service_id"], "delivered_units": None, "unit_price": None})
    return actions


def _comparable(text: Any) -> str:
    return re.sub(r"[\W_]+", " ", str(text or "").casefold()).strip()


def _contains_either(left: Any, right: Any) -> bool:
    a, b = _comparable(left), _comparable(right)
    return bool(a) and bool(b) and (a in b or b in a)


def _offer_head(offer_raw_text: Any) -> str:
    """原文开头的商品名：第一行中冒号之前的部分。"""
    return _comparable(re.split(r"[:\n]", str(offer_raw_text or ""), maxsplit=1)[0])


def still_in_evidence(offer_raw_text: Any, evidence: str) -> bool:
    """旧 offer 原文仍出现在当前内容中：本次抽取漏掉它不构成移除证据。"""
    text = _comparable(offer_raw_text)
    return bool(text) and text in _comparable(evidence)


def plan_offers(candidates: list[dict[str, Any]], existing_offers: list[dict[str, Any]],
                existing_items: dict[int, list[dict[str, Any]]],
                valid_service_ids: set[int], evidence: str) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """候选 offer 按 fingerprint 唯一匹配已有 offer；未匹配的 active 旧 offer 只有在原文已不在当前内容中时才停用，不删除。"""
    by_key: dict[str, list[dict[str, Any]]] = {}
    for old in existing_offers:
        key = _fingerprint({"business_id": old["business_id"], "offer_raw_text": old["offer_raw_text"],
                            "promotion_id": old["promotion_id"], "membership_plan_id": old["membership_plan_id"]})
        by_key.setdefault(key, []).append(old)
    actions: list[dict[str, Any]] = []
    rejected: list[dict[str, Any]] = []
    matched: set[int] = set()
    # 第二轮身份匹配：原文写法不同但一方包含另一方（同一 offer 抽取到的范围不同），且一一对应
    exact_keys = {c["fingerprint"] for c in candidates}
    loose = {c["fingerprint"]: [o for o in existing_offers
                                if _fingerprint({"business_id": o["business_id"], "offer_raw_text": o["offer_raw_text"],
                                                 "promotion_id": o["promotion_id"],
                                                 "membership_plan_id": o["membership_plan_id"]}) not in exact_keys
                                and _contains_either(o["offer_raw_text"], c["master"]["offer_raw_text"])]
             for c in candidates if c["fingerprint"] not in by_key}
    claimed: dict[int, int] = {}
    for olds in loose.values():
        for old in olds:
            claimed[old["id"]] = claimed.get(old["id"], 0) + 1
    # 第三轮身份匹配：前两轮都没对上的候选，按商品名（原文第一行；旧原文取冒号或换行之前）一一对应
    used = {o["id"] for c in candidates for o in by_key.get(c["fingerprint"], [])} | set(claimed)
    unmatched = [c for c in candidates if c["fingerprint"] not in by_key
                 and not any(claimed[o["id"]] == 1 for o in loose.get(c["fingerprint"], []))]
    old_heads: dict[str, list[dict[str, Any]]] = {}
    for old in existing_offers:
        if old["id"] not in used:
            old_heads.setdefault(_offer_head(old["offer_raw_text"]), []).append(old)
    new_heads: dict[str, int] = {}
    for candidate in unmatched:
        head = _offer_head(candidate["master"]["offer_raw_text"])
        new_heads[head] = new_heads.get(head, 0) + 1
    by_head = {head: olds[0] for head, olds in old_heads.items()
               if head and len(olds) == 1 and new_heads.get(head) == 1}
    for candidate in candidates:
        olds = by_key.get(candidate["fingerprint"])
        if olds is None:
            # 只被这一条候选包含的旧 offer：原文最长者视为同一 offer，其余是它的重复项，停用
            olds = sorted((o for o in loose.get(candidate["fingerprint"], []) if claimed[o["id"]] == 1),
                          key=lambda o: (-len(_comparable(o["offer_raw_text"])), o["id"]))
            for duplicate in olds[1:]:
                matched.add(duplicate["id"])
                if duplicate["is_active"]:
                    actions.append({"action": "deactivate", "offer_id": duplicate["id"],
                                    "offer_raw_text": duplicate["offer_raw_text"], "reason": "duplicate_of_matched_offer"})
            olds = olds[:1]
            if not olds and candidate in unmatched:
                head_match = by_head.get(_offer_head(candidate["master"]["offer_raw_text"]))
                olds = [head_match] if head_match is not None else []
        olds = olds or []
        if len(olds) > 1:
            rejected.append({"offer_raw_text": candidate["master"]["offer_raw_text"][:300],
                             "reason": "ambiguous_offer_identity", "blocking": True,
                             "offer_ids": sorted(o["id"] for o in olds)})
            matched.update(o["id"] for o in olds)
            continue
        items_actions = None
        if not olds:
            actions.append({"action": "insert", "fingerprint": candidate["fingerprint"], "master": candidate["master"],
                            "blanket": candidate["blanket"], "is_active": True,
                            "items": reconcile_items([], candidate["items"], blanket=candidate["blanket"],
                                                     valid_service_ids=valid_service_ids)})
            continue
        old = olds[0]
        matched.add(old["id"])
        items_actions = reconcile_items(existing_items.get(old["id"], []), candidate["items"],
                                        blanket=candidate["blanket"], valid_service_ids=valid_service_ids)
        patch = {column: candidate["master"][column] for column in PATCH_COLUMNS
                 if not _same(column, old[column], candidate["master"][column])}
        reactivate = not old["is_active"]
        changed = bool(patch) or reactivate or any(a["action"] != "keep" for a in items_actions)
        actions.append({"action": "update" if changed else "unchanged", "offer_id": old["id"],
                        "fingerprint": candidate["fingerprint"], "master": candidate["master"], "patch": patch,
                        "blanket": candidate["blanket"], "reactivate": reactivate, "is_active": True,
                        "items": items_actions})
    new_candidate_tokens = [set(_comparable(a["master"]["offer_raw_text"]).split()) for a in actions
                            if a["action"] == "insert"]
    for old in existing_offers:
        if not old["is_active"] or old["id"] in matched:
            continue
        old_tokens = set(_comparable(old["offer_raw_text"]).split())
        if sum(1 for tokens in new_candidate_tokens if tokens and tokens <= old_tokens) >= 2:
            # 旧 offer 把多个服务并在一条里，已被本次拆出的多条独立 offer 取代
            actions.append({"action": "deactivate", "offer_id": old["id"], "offer_raw_text": old["offer_raw_text"]})
            continue
        if still_in_evidence(old["offer_raw_text"], evidence):
            # 内容仍包含原文，本次抽取不完整：保留原 offer，并阻止标记为已抽取
            rejected.append({"offer_raw_text": str(old["offer_raw_text"])[:300], "reason": "unmatched_offer_still_in_content",
                             "blocking": True, "offer_ids": [old["id"]]})
            continue
        actions.append({"action": "deactivate", "offer_id": old["id"], "offer_raw_text": old["offer_raw_text"]})
    return actions, rejected


def _iso_or_none(value: Any) -> str | None:
    return None if value is None else value.isoformat()


def plan_source(table: str, row: dict[str, Any], payload: dict[str, Any] | None, evidence: str,
                existing_offers: list[dict[str, Any]], existing_items: dict[int, list[dict[str, Any]]],
                valid_service_ids: set[int]) -> dict[str, Any]:
    spec = SOURCE_SPEC[table]
    result = {"table": table, "id": row[spec["id"]], "business_id": row["business_id"],
              "source_url": row["source_url"], "content_hash": content_hash(table, row),
              "source_updated_at": _iso_or_none(row["updated_at"]),
              "existing_state": {str(o["id"]): _iso_or_none(o["updated_at"]) for o in existing_offers},
              "existing_item_ids": {str(o["id"]): sorted(i["offer_item_id"] for i in existing_items.get(o["id"], []))
                                    for o in existing_offers},
              "rejected": [], "offers": [], "status": "ready", "blocked_reason": None}
    active_existing = [o for o in existing_offers if o["is_active"]]
    if payload is None:
        # 内容为空：没有已有 active offer 时无需抽取；有则交人工确认，不自动停用
        if active_existing:
            result.update(status="blocked", blocked_reason="empty_content_with_active_offers")
        return result
    candidates, rejected = candidates_from_payload(payload, table, row, evidence)
    if not candidates and active_existing and (not payload["offers"] or any(r["blocking"] for r in rejected)):
        result.update(status="blocked", blocked_reason="empty_extraction_with_active_offers", rejected=rejected)
        return result
    actions, ambiguous = plan_offers(candidates, existing_offers, existing_items, valid_service_ids, evidence)
    rejected += ambiguous
    result["offers"] = actions
    result["rejected"] = rejected
    return result
