from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Any, Callable, Mapping

from utils.pipeline_db import literal
from utils.source_url import source_url_key

PROMOTION_SNAPSHOT_COLUMNS = ("promotion_id, business_id, source_url, promotion_title, promotion_content, "
                              "campaign_start_date, campaign_end_date, is_active, updated_at")
MEMBERSHIP_SNAPSHOT_COLUMNS = ("plan_id, business_id, source_url, membership_name, membership_price, billing_period, "
                               "minimum_commitment_months, benefits, updated_at")


def snapshot_tables(business_ids: list[int]) -> dict[str, tuple[str, str, str]]:
    """涉及商家的名称与现有促销、会员行的快照定义。"""
    where = f"business_id = any({literal(sorted(set(business_ids)))}::bigint[])"
    return {
        "businesses": ("master_business_info", "business_id, name", where),
        "promotions": ("clinic_promotions", PROMOTION_SNAPSHOT_COLUMNS, where),
        "memberships": ("clinic_memberships", MEMBERSHIP_SNAPSHOT_COLUMNS, where),
    }


def snapshot_tables_for_pages(pages: list[Mapping[str, Any]]) -> dict[str, tuple[str, str, str]]:
    if not pages:
        raise RuntimeError("no_pages: the decisions file selects no pages")
    return snapshot_tables([p["business_id"] for p in pages])


def load_decision_pages(payload: Mapping[str, Any], *, business_ids: set[int] | None = None,
                        limit: int | None = None) -> list[dict[str, Any]]:
    """决策文件 decisions[] 中 decision=add 的页面；kind=membership 走会员，其余（promotion/pricing/空）走促销。

    连锁品牌的多家分店共用同一个页面时，每家分店各成一页，各自写入一行促销或会员；抽取结果按同一份抓取缓存，各分店内容一致。
    """
    pages: dict[tuple[str, int], dict[str, Any]] = {}
    for record in payload["decisions"]:
        if record["decision"] != "add":
            continue
        business_id = int(record["business_id"])
        if business_ids is not None and business_id not in business_ids:
            continue
        url = record["url"]
        if (url, business_id) not in pages:
            kind = "membership" if record.get("kind") == "membership" else "promotion"
            pages[(url, business_id)] = {"url": url, "business_id": business_id, "kind": kind}
    selected = [pages[key] for key in sorted(pages)]
    return selected if limit is None else selected[:limit]


def latest_scrapes(rows: list[Mapping[str, Any]]) -> dict[str, dict[str, Any]]:
    """按 URL 身份键取最新的成功 scrape。"""
    newest: dict[str, dict[str, Any]] = {}
    for row in rows:
        if row.get("success") is not True:
            continue
        response = row.get("response_json") or {}
        response_data = response.get("data") if isinstance(response, dict) and isinstance(response.get("data"), dict) else response
        if isinstance(response_data, dict):
            row = {**row, "ocr_meta": row.get("ocr_meta") or response_data.get("ocr_meta")}
        url = source_url_key(row["source_url"])
        rank = (str(row.get("updated_at") or ""), int(row.get("id") or 0))
        current = newest.get(url)
        if current is None or rank > (str(current.get("updated_at") or ""), int(current.get("id") or 0)):
            newest[url] = dict(row)
    return newest


def _cached(cache_dir: Path, name: str, produce: Callable[[], Any]) -> Any:
    """缓存 LLM 原始回答；回答是否可用由调用方每次按确定性规则判定，判定结果不写入缓存。"""
    path = cache_dir / f"{name}.json"
    if path.exists():
        return json.loads(path.read_text())
    value = produce()
    cache_dir.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, ensure_ascii=False))
    return value


def _page_evidence(row: Mapping[str, Any]) -> str:
    """页面证据：markdown 与图片 OCR 文本。卡片链接到的条款段落不交给 LLM，由 attach_terms 追加到对应卡片。"""
    return "\n\n".join(value for value in
                       (str(row.get("markdown") or "").strip(), str(row.get("markdown_ocr") or "").strip()) if value)


def _cache_name(kind: str, row: Mapping[str, Any], evidence: str, *, llm: Any, schema: Mapping[str, Any],
                prompt: str) -> str:
    """缓存键覆盖证据、schema、提示词与模型，任一变化都重新调用 LLM。"""
    digest = hashlib.sha256("\n\x00".join(
        [evidence, json.dumps(schema, sort_keys=True), prompt, str(llm.model)]).encode()).hexdigest()[:16]
    return f"{kind}-{row['request_fingerprint']}-{digest}"


def database_state(snapshot: Mapping[str, list[dict[str, Any]]], business_ids: list[int]) -> dict[str, Any]:
    """从快照取涉及商家的名称与现有促销、会员行，并按身份键建索引；同键多行即报错。"""
    names = {r["business_id"]: r["name"] for r in snapshot["businesses"]}
    missing = sorted(set(business_ids) - set(names))
    if missing:
        raise RuntimeError(f"unknown_business_ids: {missing}")
    promotions: dict[tuple[int, str], dict[str, Any]] = {}
    for row in snapshot["promotions"]:
        index = (row["business_id"], source_url_key(row["source_url"]))
        if index in promotions:
            raise RuntimeError(f"duplicate_promotion_identity: {index}")
        promotions[index] = row
    memberships: dict[tuple[int, str, str], dict[str, Any]] = {}
    for row in snapshot["memberships"]:
        if row["source_url"] is None or row["membership_name"] is None:
            raise RuntimeError(f"membership_without_identity: plan_id={row['plan_id']} has no source_url or name")
        index = (row["business_id"], source_url_key(row["source_url"]), row["membership_name"].strip().casefold())
        if index in memberships:
            raise RuntimeError(f"duplicate_membership_identity: {index}")
        memberships[index] = row
    return {"names": names, "promotions": promotions, "memberships": memberships}
