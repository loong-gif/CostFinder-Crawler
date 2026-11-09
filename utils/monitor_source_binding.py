"""监控来源页面与商家的绑定，以及按商家拆分的来源图、404 停用计划与连锁分店合并计划。"""
from __future__ import annotations

import re
from urllib.parse import urlsplit

from utils.monitor_price_writer_plan import _protect, normalize_source_url


def check_promotion_source(url: str, snapshot: dict) -> None:
    canonical = normalize_source_url(url)
    if re.search(r"(?:^|[-_/])memberships?(?:[-_/]|$)", urlsplit(url).path, re.I):
        raise RuntimeError("membership_source_requires_review")
    for row in snapshot.get("memberships", []):
        source = row.get("source_url")
        if source:
            try:
                matches = normalize_source_url(source) == canonical
            except ValueError:
                continue
            if matches:
                raise RuntimeError("membership_source_requires_review")


def _address_key(address) -> str:
    return " ".join(re.findall(r"[a-z0-9]+", str(address or "").casefold()))


def bind_source(url: str, snapshot: dict) -> list[dict]:
    """来源页面对应的商家，按 business_id 排序。

    多家商家共用同一来源页面（连锁品牌的统一促销页）时，地址各不相同的是不同分店，每家各写一份；
    地址相同或缺失时无法区分是分店还是重复登记，交人工审查。
    """
    check_promotion_source(url, snapshot)
    canonical = normalize_source_url(url)
    matches = set()
    for key in ("promotions", "services"):
        for row in snapshot[key]:
            source = row.get("source_url")
            if source:
                try:
                    if normalize_source_url(source) == canonical:
                        matches.add(row["business_id"])
                except ValueError:
                    pass
    if not matches:
        domain = urlsplit(url).hostname.lower().removeprefix("www.")
        for row in snapshot["businesses"]:
            website = str(row.get("website") or "")
            host = urlsplit(website if "://" in website else "https://"+website).hostname
            if host and host.lower().removeprefix("www.") == domain:
                matches.add(row["business_id"])
    if not matches:
        raise RuntimeError("source_business_missing")
    if len(matches) > 1:
        addresses = {row["business_id"]: _address_key(row.get("address")) for row in snapshot["businesses"]
                     if row["business_id"] in matches}
        keys = [addresses.get(business_id, "") for business_id in matches]
        if not all(keys) or len(set(keys)) != len(keys):
            raise RuntimeError("source_business_ambiguous")
    return [{"business_id":business_id, "url":url} for business_id in sorted(matches)]


def not_found_plan(source: dict, graph: dict, protections: dict) -> dict:
    """页面确认 404：停用该商家在这个来源上唯一的有效促销及其 offer。"""
    active = [p for p in graph["promotions"] if p.get("is_active") is True]
    if len(active) != 1:
        raise RuntimeError("not_found_source_match_requires_review")
    parent_id = active[0]["promotion_id"]
    operations = [{"table":"promo_offer_master","action":"update","id":o["id"],
                   "values":{"is_active":False}} for o in graph["offers"]
                  if o.get("is_active") is True and o["promotion_id"] == parent_id]
    operations.append({"table":"clinic_promotions","action":"update","id":parent_id,
                       "values":{"is_active":False}})
    _protect(graph,operations,protections)
    return {"schema_version":1,"status":"ready","source":source,"before":graph,
            "operations":operations,"review":[],"evidence_summary":{"confirmed_http_and_visible_404":True},
            "image_policy":{"version":1,"depends_on_images":False,"image_write_enabled":False}}


def combine_branch_plans(url: str, plans: list[dict]) -> dict:
    """连锁分店共用同一来源页面：每家一份计划，全部就绪才写入；审查原因注明分店。"""
    combined = {"schema_version":1, "status":"ready" if all(p["status"] == "ready" for p in plans) else "review",
                "source":{"url":url, "business_ids":[p["source"]["business_id"] for p in plans]},
                "branches":plans, "operations":[op for p in plans for op in p["operations"]],
                "review":[{**item, "business_id":p["source"]["business_id"]} for p in plans for item in p.get("review", [])],
                "event_id":plans[0]["event_id"], "evidence_path":plans[0]["evidence_path"],
                "prepared_at":plans[0]["prepared_at"]}
    image_retry = [p["image_retry_reason"] for p in plans if p.get("image_retry_reason")]
    if image_retry:
        combined["image_retry_reason"] = image_retry[0]
    return combined


def snapshot_business_graph(snapshot: dict, business_id: int) -> dict:
    promotions = [r for r in snapshot["promotions"] if r["business_id"] == business_id]
    promotion_ids = {r["promotion_id"] for r in promotions}
    offers = [r for r in snapshot["offers"] if r["business_id"] == business_id or r.get("promotion_id") in promotion_ids]
    offer_ids = {r["id"] for r in offers}
    return {"promotions":promotions, "offers":offers,
            "items":[r for r in snapshot["items"] if r["offer_id"] in offer_ids],
            "services":[r for r in snapshot["services"] if r["business_id"] == business_id],
            "terms":[r for r in snapshot.get("terms", []) if r.get("offer_id") in offer_ids]}
