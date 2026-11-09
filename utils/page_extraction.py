"""原始爬取结果到 clinic_promotions / clinic_memberships：由快照生成冻结计划，渲染为单个 DO 块按身份键 upsert。"""
from __future__ import annotations

import hashlib
import json
from collections import defaultdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Mapping

from psycopg.types.json import Jsonb

from utils.page_extraction_common import _campaign_date, _rejection, index_manifest
from utils.page_extraction_memberships import _plan_memberships
from utils.page_extraction_promotions import _plan_promotions
from utils.page_extraction_snapshot import _page_evidence
from utils.page_extraction_source_review import SourceReviewValidationError, _source_review, verified_source_review
from utils.pipeline_db import LOCK_PROMOTIONS, NEXT_PROMOTION_ID, do_block, literal, plan_digest, render
from utils.promo_extraction import comparison_key
from utils.promotion_rules import check_promotion
from utils.source_url import source_url_key

PROMOTION_UPSERT = """
insert into public.clinic_promotions (promotion_id, business_id, source_url, promotion_title, promotion_content,
    campaign_start_date, campaign_end_date, is_active)
values (""" + NEXT_PROMOTION_ID + """, {business_id}, {source_url}, {title}, {content}, {start}, {end}, true)
on conflict (business_id, public.fn_source_url_key(source_url)) do update set
    promotion_title = excluded.promotion_title,
    promotion_content = excluded.promotion_content,
    campaign_start_date = excluded.campaign_start_date,
    campaign_end_date = excluded.campaign_end_date,
    is_active = excluded.is_active,
    updated_at = now()
returning (xmax = 0) into v_inserted;
"""

MEMBERSHIP_UPSERT = """
insert into public.clinic_memberships (business_id, source_url, membership_name, membership_price,
    billing_period, minimum_commitment_months, benefits)
values ({business_id}, {source_url}, {name}, {price}, {billing_period}, {months}, {benefits})
on conflict (business_id, public.fn_source_url_key(source_url), lower(btrim(membership_name))) do update set
    membership_price = excluded.membership_price,
    billing_period = excluded.billing_period,
    minimum_commitment_months = excluded.minimum_commitment_months,
    benefits = coalesce(excluded.benefits, clinic_memberships.benefits),
    updated_at = now()
returning (xmax = 0) into v_inserted;
"""


def build_plan(pages: list[dict[str, Any]], *, scrapes: Mapping[str, Mapping[str, Any]], state: Mapping[str, Any],
               llm: Any, fetch_title: Callable[[str, str], str], cache_dir: Path,
               manifest: Mapping[str, Any] | None, promotion_schema: dict[str, Any],
               membership_schema: dict[str, Any]) -> dict[str, Any]:
    manifest_sha256 = None
    if manifest is not None:
        manifest_sha256 = hashlib.sha256(json.dumps(manifest, sort_keys=True).encode()).hexdigest()
        manifest = index_manifest(manifest)
    promotion_pages: list[dict[str, Any]] = []
    membership_pages: list[dict[str, Any]] = []
    invalid_source_reviews: list[dict[str, Any]] = []
    for page in pages:
        row = scrapes.get(source_url_key(page["url"]))
        if row is None:
            (membership_pages if page["kind"] == "membership" else promotion_pages).append(page)
            continue
        evidence_key = comparison_key(_page_evidence(row))
        source_review = _source_review(page, row, llm=llm, cache_dir=cache_dir)
        try:
            review = verified_source_review(source_review, evidence_key)
        except SourceReviewValidationError:
            kind = "membership" if page["kind"] == "membership" else "promotion"
            invalid_source_reviews.append(_rejection(kind, page["business_id"], page["url"], "source_review_invalid"))
            continue
        page = {**page, "_source_review": review, "_evidence_key": evidence_key}
        if page["kind"] == "membership":
            # 会员页面只写会员表，页面上的促销卡片不进促销表
            membership_pages.append(page)
            continue
        if review["page_type"] in {"promotion", "mixed"}:
            promotion_pages.append(page)
        if review["page_type"] in {"membership", "mixed"}:
            membership_pages.append(page)
        if review["page_type"] not in {"promotion", "membership", "mixed"}:
            (membership_pages if page["kind"] == "membership" else promotion_pages).append(page)
    entries = invalid_source_reviews + _plan_promotions(
        promotion_pages, scrapes=scrapes, names=state["names"],
        existing=state["promotions"], llm=llm, schema=promotion_schema, cache_dir=cache_dir, manifest=manifest,
        fetch_title=fetch_title)
    entries += _plan_memberships(
        membership_pages, scrapes=scrapes, existing=state["memberships"], cities=state.get("cities", {}),
        llm=llm, schema=membership_schema, cache_dir=cache_dir, manifest=manifest)
    counts: dict[str, int] = defaultdict(int)
    for entry in entries:
        counts[f"{entry['kind']}:{entry['action']}"] += 1
    return {"generated_at": datetime.now(timezone.utc).isoformat(), "visibility_manifest_sha256": manifest_sha256,
            "pages": len(pages), "summary": dict(sorted(counts.items())), "entries": entries}


def _row_where(entry: Mapping[str, Any]) -> tuple[str, str]:
    """目标行的表名与身份键条件。"""
    if entry["kind"] == "promotion":
        return "clinic_promotions", render(
            "business_id = {business_id} and public.fn_source_url_key(source_url) = public.fn_source_url_key({url})",
            business_id=entry["business_id"], url=entry["source_url"])
    return "clinic_memberships", render(
        "business_id = {business_id} and public.fn_source_url_key(source_url) = public.fn_source_url_key({url}) "
        "and lower(btrim(membership_name)) = lower(btrim({name}))",
        business_id=entry["business_id"], url=entry["source_url"], name=entry["membership_name"])


def _expect_target_row(entry: Mapping[str, Any], label: str) -> list[str]:
    """核对目标行：计划时不存在的行仍不存在，存在的行 updated_at 与计划一致（并加行锁）。
    旧数据中 updated_at 可能为 NULL，按 NULL 比较。"""
    table, where = _row_where(entry)
    failure = f"raise exception {literal('stale_plan: ' + label)};"
    if entry["action"] == "insert":
        return [f"perform 1 from public.{table} where {where};", f"if found then {failure} end if;"]
    return [f"select updated_at into v_updated from public.{table} where {where} for update;",
            render("if not found or v_updated is distinct from {ts}::timestamptz then ",
                   ts=entry["expected_updated_at"]) + failure + " end if;"]


def _entry_statements(entry: Mapping[str, Any]) -> list[str]:
    """一条 insert/update 条目的 plpgsql 语句：重新执行促销写入规则，核对目标行，执行 upsert，核对写入结果。"""
    label = f"{entry['kind']} {entry['source_url']}"
    if entry["kind"] == "promotion":
        end = _campaign_date(entry["campaign_end_date"])
        check_promotion(entry["source_url"], entry["title"], entry["content"], is_active=True, campaign_end_date=end)
        upsert = render(PROMOTION_UPSERT, business_id=entry["business_id"], source_url=entry["source_url"],
                        title=entry["title"], content=Jsonb(entry["content"]),
                        start=_campaign_date(entry["campaign_start_date"]), end=end)
    else:
        benefits = entry["benefits"]
        upsert = render(MEMBERSHIP_UPSERT, business_id=entry["business_id"], source_url=entry["source_url"],
                        name=entry["membership_name"], price=entry["membership_price"],
                        billing_period=entry["billing_period"], months=entry["minimum_commitment_months"],
                        benefits=None if benefits is None else Jsonb(benefits))
    inserted = entry["action"] == "insert"
    outcome = "stale_plan: " + label + (" was not inserted" if inserted else " was inserted instead of updated")
    return [*_expect_target_row(entry, label), upsert.strip(),
            f"if v_inserted is distinct from {literal(inserted)} then raise exception {literal(outcome)}; end if;"]


def _branch_statements(entries: list[Mapping[str, Any]]) -> list[str]:
    """连锁分店共用同一页面、写入内容完全相同的多条促销（同为 insert 或同为 update）：按商家循环写入，内容只出现一次。
    循环内逐家核对：insert 核对计划时该行不存在、写入结果为新插入；update 锁定该行、核对 updated_at 与计划一致、
    写入结果为更新。"""
    entry = entries[0]
    inserted = entry["action"] == "insert"
    end = _campaign_date(entry["campaign_end_date"])
    check_promotion(entry["source_url"], entry["title"], entry["content"], is_active=True, campaign_end_date=end)
    label = f"promotion {entry['source_url']}"
    upsert = render(PROMOTION_UPSERT.replace("{business_id}", "v_business_id"), source_url=entry["source_url"],
                    title=entry["title"], content=Jsonb(entry["content"]),
                    start=_campaign_date(entry["campaign_start_date"]), end=end)
    where = render("business_id = v_business_id and public.fn_source_url_key(source_url) = "
                   "public.fn_source_url_key({url})", url=entry["source_url"])
    ordered = sorted(entries, key=lambda e: e["business_id"])
    statements = [render("for v_i in 1..{n} loop", n=len(ordered)),
                  render("v_business_id := ({ids}::bigint[])[v_i];", ids=[e["business_id"] for e in ordered])]
    if inserted:
        statements += [f"perform 1 from public.clinic_promotions where {where};",
                       f"if found then raise exception {literal('stale_plan: ' + label)}; end if;"]
    else:
        statements += [f"select updated_at into v_updated from public.clinic_promotions where {where} for update;",
                       render("if not found or v_updated is distinct from ({ts}::timestamptz[])[v_i] then ",
                              ts=[e["expected_updated_at"] for e in ordered])
                       + f"raise exception {literal('stale_plan: ' + label)}; end if;"]
    outcome = "stale_plan: " + label + (" was not inserted" if inserted else " was inserted instead of updated")
    return statements + [upsert.strip(),
                         f"if v_inserted is distinct from {literal(inserted)} then raise exception "
                         f"{literal(outcome)}; end if;",
                         "end loop;"]


def render_apply(plan: Mapping[str, Any], manifest: Mapping[str, Any]) -> str:
    """全部 insert/update 条目渲染为单个 DO 块；核对可见性清单哈希，并重新执行促销写入规则。"""
    if plan["visibility_manifest_sha256"] is None:
        raise RuntimeError("plan_without_visibility_manifest: regenerate the plan with --visibility-manifest")
    if hashlib.sha256(json.dumps(manifest, sort_keys=True).encode()).hexdigest() != plan["visibility_manifest_sha256"]:
        raise RuntimeError("visibility_manifest_changed: the manifest differs from the one used for planning")
    writes = [e for e in plan["entries"] if e["action"] in ("insert", "update")]
    statements = [LOCK_PROMOTIONS] if any(e["kind"] == "promotion" for e in writes) else []
    counts: dict[str, int] = defaultdict(int)
    branches: dict[str, list[Mapping[str, Any]]] = defaultdict(list)
    for entry in writes:
        if entry["kind"] == "promotion":
            same = json.dumps([entry[k] for k in ("action", "source_url", "title", "content", "campaign_start_date",
                                                  "campaign_end_date")], ensure_ascii=False)
            branches[same].append(entry)
    for group in branches.values():
        if len(group) > 1:
            statements.extend(_branch_statements(group))
            counts[f"promotion:{group[0]['action']}"] += len(group)
    grouped = {id(e) for group in branches.values() if len(group) > 1 for e in group}
    for entry in writes:
        if id(entry) in grouped:
            continue
        statements.extend(_entry_statements(entry))
        counts[f"{entry['kind']}:{entry['action']}"] += 1
    header = (f"extract-pages apply\nplan sha256 {plan_digest(plan)}\n"
              f"visibility manifest sha256 {plan['visibility_manifest_sha256']}\n"
              f"writes {json.dumps(dict(sorted(counts.items())))}")
    return do_block(statements, declare=["v_updated timestamptz", "v_inserted boolean", "v_business_id bigint",
                                         "v_i int"], header=header)
