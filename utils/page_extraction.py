"""原始爬取结果到 clinic_promotions / clinic_memberships：由快照生成冻结计划，渲染为单个 DO 块按身份键 upsert。"""
from __future__ import annotations

import hashlib
import json
import re
from collections import defaultdict
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Any, Callable, Mapping

from psycopg.types.json import Jsonb

from scripts.run_domain_architecture_pipeline import llm_extract
from utils.membership_evidence import prepare_membership_write
from utils.page_title import TitleUnavailable, choose_title, title_candidates_from_scrape
from utils.pipeline_db import LOCK_PROMOTIONS, NEXT_PROMOTION_ID, do_block, literal, plan_digest, render
from utils.promo_extraction import SYSTEM_PROMPT as PROMOTION_SYSTEM_PROMPT
from utils.promo_extraction import llm_extract_promotions, merge_content_blocks, merge_promotion_items
from utils.promotion_rules import PromotionRejected, check_promotion, today
from utils.recent_raw_extraction import promotion_evidence_markdown
from utils.rendered_visibility_gate import filter_visible_promotion_content
from utils.source_url import hub_url, source_url_key

PROMOTION_KINDS = {"promotion", "pricing", ""}
MEMBERSHIP_TASK = "Extract purchasable membership plans with explicit prices."
NO_VALIDATED_PROMOTIONS = "no validated promotions"

PROMOTION_UPSERT = """
insert into public.clinic_promotions (promotion_id, business_id, source_url, promotion_title, promotion_content,
    campaign_start_date, campaign_end_date, is_active, is_offer_extracted)
values (""" + NEXT_PROMOTION_ID + """, {business_id}, {source_url}, {title}, {content}, {start}, {end}, true, false)
on conflict (business_id, public.fn_source_url_key(source_url)) do update set
    promotion_title = excluded.promotion_title,
    promotion_content = excluded.promotion_content,
    campaign_start_date = excluded.campaign_start_date,
    campaign_end_date = excluded.campaign_end_date,
    is_active = excluded.is_active,
    is_offer_extracted = case
        when clinic_promotions.promotion_content is distinct from excluded.promotion_content then false
        else clinic_promotions.is_offer_extracted end,
    updated_at = now()
returning (xmax = 0) into v_inserted;
"""

MEMBERSHIP_UPSERT = """
insert into public.clinic_memberships (business_id, source_url, membership_name, membership_price,
    billing_period, minimum_commitment_months, benefits, is_offer_extracted)
values ({business_id}, {source_url}, {name}, {price}, {billing_period}, {months}, {benefits}, false)
on conflict (business_id, public.fn_source_url_key(source_url), lower(btrim(membership_name))) do update set
    membership_price = excluded.membership_price,
    billing_period = excluded.billing_period,
    minimum_commitment_months = excluded.minimum_commitment_months,
    benefits = coalesce(excluded.benefits, clinic_memberships.benefits),
    is_offer_extracted = case
        when excluded.benefits is not null and excluded.benefits is distinct from clinic_memberships.benefits then false
        else clinic_memberships.is_offer_extracted end,
    updated_at = now()
returning (xmax = 0) into v_inserted;
"""

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
    """决策文件 decisions[] 中 decision=add 的页面；kind=membership 走会员，其余（promotion/pricing/空）走促销。"""
    pages: dict[str, dict[str, Any]] = {}
    for record in payload["decisions"]:
        if record["decision"] != "add":
            continue
        business_id = int(record["business_id"])
        if business_ids is not None and business_id not in business_ids:
            continue
        url = record["url"]
        previous = pages.get(url)
        if previous is not None and previous["business_id"] != business_id:
            raise RuntimeError(f"ambiguous_business: {url} belongs to {previous['business_id']} and {business_id}")
        if previous is None:
            kind = "membership" if record.get("kind") == "membership" else "promotion"
            pages[url] = {"url": url, "business_id": business_id, "kind": kind}
    selected = [pages[url] for url in sorted(pages)]
    return selected if limit is None else selected[:limit]


def latest_scrapes(rows: list[Mapping[str, Any]]) -> dict[str, dict[str, Any]]:
    """按 URL 身份键取最新的成功 scrape。"""
    newest: dict[str, dict[str, Any]] = {}
    for row in rows:
        if row.get("success") is not True:
            continue
        url = source_url_key(row["source_url"])
        rank = (str(row.get("updated_at") or ""), int(row.get("id") or 0))
        current = newest.get(url)
        if current is None or rank > (str(current.get("updated_at") or ""), int(current.get("id") or 0)):
            newest[url] = dict(row)
    return newest


def _cached(cache_dir: Path, name: str, produce: Callable[[], Any]) -> Any:
    path = cache_dir / f"{name}.json"
    if path.exists():
        return json.loads(path.read_text())
    value = produce()
    cache_dir.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, ensure_ascii=False))
    return value


def _cache_name(kind: str, row: Mapping[str, Any], evidence: str, *, llm: Any, schema: Mapping[str, Any],
                prompt: str) -> str:
    """缓存键覆盖证据、schema、提示词与模型，任一变化都重新调用 LLM。"""
    digest = hashlib.sha256("\n\x00".join(
        [evidence, json.dumps(schema, sort_keys=True), prompt, str(llm.model)]).encode()).hexdigest()[:16]
    return f"{kind}-{row['request_fingerprint']}-{digest}"


def _campaign_date(value: str | None) -> date | None:
    """抽取结果的日期已由 promo_extraction 校验为 YYYY-MM-DD 或 null。"""
    return date.fromisoformat(value) if value else None


def _iso(value: date | None) -> str | None:
    return value.isoformat() if value else None


def _merged_dates(items: list[Mapping[str, Any]]) -> tuple[date | None, date | None]:
    starts = [i["start"] for i in items]
    ends = [i["end"] for i in items]
    return (None if None in starts else min(starts)), (None if None in ends else max(ends))


def _rejection(kind: str, business_id: int, url: str, reason: str, **extra: Any) -> dict[str, Any]:
    return {"kind": kind, "business_id": business_id, "source_url": url,
            "target_key": {"business_id": business_id, "source_url_key": source_url_key(url)},
            "action": "rejected", "reason": reason, **extra}


def index_manifest(manifest: Mapping[str, Any]) -> dict[str, Mapping[str, Any]]:
    """可见性清单按 URL 身份键索引。"""
    return {source_url_key(url): value for url, value in manifest.items()}


def _visibility_for(manifest: Mapping[str, Any] | None, url: str) -> Mapping[str, Any] | None:
    if manifest is None:
        return None
    return manifest.get(source_url_key(url))


def _extract_promotion_page(page: Mapping[str, Any], row: Mapping[str, Any], *, llm: Any, schema: dict[str, Any],
                            cache_dir: Path, manifest: Mapping[str, Any] | None) -> dict[str, Any]:
    """返回 {content,start,end} 或 {reject: code}。标题不在此处理。"""
    if manifest is not None and _visibility_for(manifest, page["url"]) is None:
        return {"reject": "not_in_visibility_manifest"}
    evidence = promotion_evidence_markdown(row)
    name = _cache_name("promotion", row, evidence, llm=llm, schema=schema, prompt=PROMOTION_SYSTEM_PROMPT)
    payload = _cached(cache_dir, name, lambda: llm_extract_promotions(
        llm, source_url=page["url"], evidence=evidence, schema=schema))
    items = payload.get("promotions") or []
    if not items:
        return {"reject": "no_promotions"}
    try:
        merged = merge_promotion_items(items, evidence, source_url=page["url"])
    except ValueError as exc:
        if str(exc) != NO_VALIDATED_PROMOTIONS:
            raise
        return {"reject": "no_validated_promotions"}
    blocks = merged["promotion_blocks"]
    if manifest is not None:
        gate = filter_visible_promotion_content(merged["promotion_content"], _visibility_for(manifest, page["url"]))
        if not gate.accepted:
            return {"reject": gate.reason}
        # 可见性按行判定；各优惠块只保留渲染后可见的行
        visible = set(merge_content_blocks([[line] for line in gate.content]))
        blocks = [[line for line in merge_content_blocks([block]) if line in visible] for block in blocks]
    return {"blocks": blocks, "start": _campaign_date(merged["campaign_start_date"]),
            "end": _campaign_date(merged["campaign_end_date"])}


def _group_title(target_url: str, is_hub: bool, pages: list[Mapping[str, Any]], scrapes: Mapping[str, Mapping[str, Any]],
                 business_name: str, fetch_title: Callable[[str, str], str]) -> str:
    """标题只取自页面 <title>：hub 取 hub 页面，其他取页面自身；raw 中没有 scrape 的 hub 现抓。"""
    sources = [scrapes.get(source_url_key(target_url))]
    if not is_hub:
        sources += [scrapes.get(source_url_key(p["url"])) for p in pages]
    sources = [s for s in sources if s is not None]
    if not sources:
        return fetch_title(target_url, business_name)
    for scrape in sources:
        try:
            return choose_title(title_candidates_from_scrape(scrape), business_name)
        except TitleUnavailable:
            continue
    raise TitleUnavailable("no_usable_title")


def _plan_promotions(pages: list[dict[str, Any]], *, scrapes, names, existing, llm, schema, cache_dir, manifest,
                     fetch_title) -> list[dict[str, Any]]:
    entries: list[dict[str, Any]] = []
    extracted: dict[tuple[int, str], list[dict[str, Any]]] = defaultdict(list)
    for page in pages:
        row = scrapes.get(source_url_key(page["url"]))
        if row is None:
            entries.append(_rejection("promotion", page["business_id"], page["url"], "no_scrape"))
            continue
        result = _extract_promotion_page(page, row, llm=llm, schema=schema, cache_dir=cache_dir, manifest=manifest)
        if "reject" in result:
            entries.append(_rejection("promotion", page["business_id"], page["url"], result["reject"]))
            continue
        # 每个页面单独判定过期，过期页面的内容不并入 hub
        if result["end"] is not None and result["end"] < today():
            entries.append(_rejection("promotion", page["business_id"], page["url"], "expired_campaign",
                                      campaign_end_date=_iso(result["end"])))
            continue
        hub = hub_url(page["url"])
        target = hub or page["url"]
        extracted[(page["business_id"], source_url_key(target))].append(
            {**page, **result, "hub": hub, "fingerprint": row["request_fingerprint"]})

    for (business_id, key), members in sorted(extracted.items()):
        members.sort(key=lambda m: m["url"])
        hub = next((m["hub"] for m in members if m["hub"]), None)
        exact = [m for m in members if source_url_key(m["url"]) == key]
        target_url = exact[0]["url"] if exact else hub
        old = existing.get((business_id, key))
        content: list[str] = []
        start, end = _merged_dates(members)
        if hub is not None and old is not None:
            # hub 行由多个子页面组成，本批只覆盖部分子页面时保留库中已有内容与已知日期
            content.extend(old["promotion_content"] or [])
            start = start or old["campaign_start_date"]
            end = end or old["campaign_end_date"]
        # 库中已有内容算一块，各页面的每个优惠各算一块
        content = merge_content_blocks([content] + [block for member in members for block in member["blocks"]])
        base = {"kind": "promotion", "business_id": business_id, "source_url": target_url,
                "target_key": {"business_id": business_id, "source_url_key": key},
                "source_pages": [m["url"] for m in members], "scrape_fingerprints": [m["fingerprint"] for m in members],
                "campaign_start_date": _iso(start), "campaign_end_date": _iso(end)}
        try:
            title = _group_title(target_url, hub is not None, members, scrapes, names[business_id], fetch_title)
        except TitleUnavailable as exc:
            entries.append({**base, "title": None, "content": content, "action": "rejected", "reason": str(exc)})
            continue
        base = {**base, "title": title, "content": content}
        try:
            check_promotion(target_url, title, content, is_active=True, campaign_end_date=end)
        except PromotionRejected as exc:
            entries.append({**base, "action": "rejected", "reason": exc.code})
            continue
        if old is None:
            entries.append({**base, "action": "insert", "reason": "new_row"})
            continue
        same = (old["promotion_title"] == title and list(old["promotion_content"] or []) == content
                and old["campaign_start_date"] == start and old["campaign_end_date"] == end and old["is_active"])
        entries.append({**base, "action": "unchanged" if same else "update",
                        "reason": "identical" if same else "row_differs",
                        "promotion_id": old["promotion_id"], "expected_updated_at": old["updated_at"].isoformat(),
                        "content_changed": list(old["promotion_content"] or []) != content})
    return entries


def _plan_memberships(pages: list[dict[str, Any]], *, scrapes, existing, llm, schema, cache_dir,
                      manifest) -> list[dict[str, Any]]:
    entries: list[dict[str, Any]] = []
    accepted: dict[tuple[int, str, str], dict[str, Any]] = {}
    conflicted: set[tuple[int, str, str]] = set()
    for page in pages:
        row = scrapes.get(source_url_key(page["url"]))
        if row is None:
            entries.append(_rejection("membership", page["business_id"], page["url"], "no_scrape"))
            continue
        if manifest is not None and _visibility_for(manifest, page["url"]) is None:
            entries.append(_rejection("membership", page["business_id"], page["url"], "not_in_visibility_manifest"))
            continue
        evidence = str(row.get("markdown") or "")
        name = _cache_name("membership", row, evidence, llm=llm, schema=schema, prompt=MEMBERSHIP_TASK)
        payload = _cached(cache_dir, name, lambda: llm_extract(
            llm, schema, task=MEMBERSHIP_TASK, source_url=page["url"], markdown=evidence))
        items = payload.get("memberships") or []
        if not items:
            entries.append(_rejection("membership", page["business_id"], page["url"], "no_memberships"))
            continue
        source_url = page["url"].strip().rstrip("/")
        key = source_url_key(source_url)
        for item in items:
            name = str(item.get("membership_name") or "").strip()
            decision = prepare_membership_write(item, evidence, _visibility_for(manifest, page["url"]) if manifest is not None else None)
            if not decision["accepted"]:
                entries.append(_rejection("membership", page["business_id"], page["url"], decision["reason"],
                                          membership_name=name))
                continue
            identity = (page["business_id"], key, name.casefold())
            candidate = {"kind": "membership", "business_id": page["business_id"], "source_url": source_url,
                         "target_key": {"business_id": page["business_id"], "source_url_key": key,
                                        "membership_name_key": name.casefold()},
                         "membership_name": name, "membership_price": decision["membership_price"],
                         "billing_period": item.get("billing_period"),
                         "minimum_commitment_months": item.get("minimum_commitment_months"),
                         "benefits": decision["benefits"], "source_pages": [page["url"]],
                         "scrape_fingerprints": [row["request_fingerprint"]]}
            if identity in conflicted:
                entries.append({**candidate, "action": "rejected", "reason": "conflicting_duplicate_membership"})
                continue
            previous = accepted.get(identity)
            if previous is None:
                accepted[identity] = candidate
                continue
            same_terms = all(previous[f] == candidate[f] for f in
                             ("membership_price", "billing_period", "minimum_commitment_months"))
            if not same_terms:
                # 同一会员出现互相矛盾的条款时全部拒绝，结果与页面顺序无关
                conflicted.add(identity)
                accepted.pop(identity)
                entries.extend({**c, "action": "rejected", "reason": "conflicting_duplicate_membership"}
                               for c in (previous, candidate))
                continue
            if candidate["benefits"] is not None:
                merged = list(previous["benefits"] or [])
                merged.extend(b for b in candidate["benefits"] if b not in merged)
                previous["benefits"] = merged
            previous["source_pages"].extend(candidate["source_pages"])
            previous["scrape_fingerprints"].extend(candidate["scrape_fingerprints"])

    for identity, entry in sorted(accepted.items()):
        old = existing.get(identity)
        if old is None:
            entries.append({**entry, "action": "insert", "reason": "new_row"})
            continue
        old_price = None if old["membership_price"] is None else float(old["membership_price"])
        same = (old_price == entry["membership_price"]
                and old["billing_period"] == entry["billing_period"]
                and old["minimum_commitment_months"] == entry["minimum_commitment_months"]
                and (entry["benefits"] is None or old["benefits"] == entry["benefits"]))
        entries.append({**entry, "action": "unchanged" if same else "update",
                        "reason": "identical" if same else "row_differs", "plan_id": old["plan_id"],
                        "expected_updated_at": old["updated_at"].isoformat()})
    return entries


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


def build_plan(pages: list[dict[str, Any]], *, scrapes: Mapping[str, Mapping[str, Any]], state: Mapping[str, Any],
               llm: Any, fetch_title: Callable[[str, str], str], cache_dir: Path,
               manifest: Mapping[str, Any] | None, promotion_schema: dict[str, Any],
               membership_schema: dict[str, Any]) -> dict[str, Any]:
    manifest_sha256 = None
    if manifest is not None:
        manifest_sha256 = hashlib.sha256(json.dumps(manifest, sort_keys=True).encode()).hexdigest()
        manifest = index_manifest(manifest)
    entries = _plan_promotions(
        [p for p in pages if p["kind"] == "promotion"], scrapes=scrapes, names=state["names"],
        existing=state["promotions"], llm=llm, schema=promotion_schema, cache_dir=cache_dir, manifest=manifest,
        fetch_title=fetch_title)
    entries += _plan_memberships(
        [p for p in pages if p["kind"] == "membership"], scrapes=scrapes, existing=state["memberships"],
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
    """核对目标行：计划时不存在的行仍不存在，存在的行 updated_at 与计划一致（并加行锁）。"""
    table, where = _row_where(entry)
    failure = f"raise exception {literal('stale_plan: ' + label)};"
    expected = entry.get("expected_updated_at")
    if expected is None:
        return [f"perform 1 from public.{table} where {where};", f"if found then {failure} end if;"]
    return [f"select updated_at into v_updated from public.{table} where {where} for update;",
            render("if not found or v_updated is distinct from {ts}::timestamptz then ", ts=expected) + failure + " end if;"]


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


def render_apply(plan: Mapping[str, Any], manifest: Mapping[str, Any]) -> str:
    """全部 insert/update 条目渲染为单个 DO 块；核对可见性清单哈希，并重新执行促销写入规则。"""
    if plan["visibility_manifest_sha256"] is None:
        raise RuntimeError("plan_without_visibility_manifest: regenerate the plan with --visibility-manifest")
    if hashlib.sha256(json.dumps(manifest, sort_keys=True).encode()).hexdigest() != plan["visibility_manifest_sha256"]:
        raise RuntimeError("visibility_manifest_changed: the manifest differs from the one used for planning")
    writes = [e for e in plan["entries"] if e["action"] in ("insert", "update")]
    statements = [LOCK_PROMOTIONS] if any(e["kind"] == "promotion" for e in writes) else []
    counts: dict[str, int] = defaultdict(int)
    for entry in writes:
        statements.extend(_entry_statements(entry))
        counts[f"{entry['kind']}:{entry['action']}"] += 1
    header = (f"extract-pages apply\nplan sha256 {plan_digest(plan)}\n"
              f"visibility manifest sha256 {plan['visibility_manifest_sha256']}\n"
              f"writes {json.dumps(dict(sorted(counts.items())))}")
    return do_block(statements, declare=["v_updated timestamptz", "v_inserted boolean"], header=header)
