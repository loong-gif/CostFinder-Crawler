"""数据质量修复：把冻结的补丁渲染为每个业务一个 DO 块（pending SQL），CAS 核对与写后核对都在 SQL 中；Python 不写库。"""
from __future__ import annotations

from copy import deepcopy
from pathlib import Path

from utils.data_quality_audit import TABLES, _protected
from utils.monitor_price_writer_plan import BLANKET_CHECK_SQL
from utils.pending_sql import write_pending
from utils.pipeline_db import do_block, literal, plan_digest
from utils.sql_checks import (
    id_array, jsonb_literal, raise_if, row_matches, sql_value, versions_differ, write_value,
)

GROUP_TABLES = {**TABLES, "terms": ("promo_offer_terms", "offer_id")}


def fetch_all(client, table, pk, *, filters=None):
    rows, offset = [], 0
    while True:
        page = client.fetch_rows(table, "*", filters=filters, order=f"{pk}.asc", limit=500, offset=offset)
        rows.extend(page)
        if len(page) < 500:
            return rows
        offset += len(page)


def read_snapshot(client):
    result = {group: fetch_all(client, table, pk) for group, (table, pk) in TABLES.items()}
    result["terms"] = fetch_all(client, "promo_offer_terms", "offer_id")
    return result


def scope(snapshot, business_id):
    result = {group: [r for r in snapshot.get(group, []) if r.get("business_id") == business_id]
              for group in ("promotions", "memberships", "services")}
    promotion_ids = {r["promotion_id"] for r in result["promotions"]}
    membership_ids = {r["plan_id"] for r in result["memberships"]}
    result["offers"] = [r for r in snapshot.get("offers", []) if r.get("business_id") == business_id
                        or r.get("promotion_id") in promotion_ids or r.get("membership_plan_id") in membership_ids]
    offer_ids = {r["id"] for r in result["offers"]}
    result["items"] = [r for r in snapshot.get("items", []) if r["offer_id"] in offer_ids]
    result["terms"] = [r for r in snapshot.get("terms", []) if r["offer_id"] in offer_ids]
    return result


def _scope_conditions(business_id):
    """各表在 SQL 中的范围条件（别名 t），与 scope() 一致。"""
    offers = (f"o.business_id = {business_id} "
              f"or o.promotion_id in (select promotion_id from public.clinic_promotions where business_id = {business_id}) "
              f"or o.membership_plan_id in (select plan_id from public.clinic_memberships where business_id = {business_id})")
    offer_ids = f"select o.id from public.promo_offer_master o where {offers}"
    return {
        "promotions": f"t.business_id = {business_id}",
        "memberships": f"t.business_id = {business_id}",
        "services": f"t.business_id = {business_id}",
        "offers": f"t.id in ({offer_ids})",
        "items": f"t.offer_id in ({offer_ids})",
        "terms": f"t.offer_id in ({offer_ids})",
    }


def _affected(frozen, repairs):
    """补丁行及触发器可能连带改动的行（子行、父行）。"""
    ids = {group: set() for group in GROUP_TABLES}
    offers_by_id = {o["id"]: o for o in frozen["offers"]}
    for repair in repairs:
        group = next(g for g, (name, _) in GROUP_TABLES.items() if name == repair["table"])
        ids[group].add(repair["id"])
        if group == "promotions":
            offer_ids = {o["id"] for o in frozen["offers"] if o.get("promotion_id") == repair["id"]}
        elif group == "memberships":
            offer_ids = {o["id"] for o in frozen["offers"] if o.get("membership_plan_id") == repair["id"]}
        elif group == "offers":
            offer_ids = {repair["id"]}
        elif group == "items":
            offer_ids = {next(i["offer_id"] for i in frozen["items"] if i["offer_item_id"] == repair["id"])}
        elif group == "services":
            offer_ids = {i["offer_id"] for i in frozen["items"] if i.get("service_id") == repair["id"]}
        else:
            offer_ids = {repair["id"]}
        for offer_id in offer_ids:
            offer = offers_by_id[offer_id]
            ids["offers"].add(offer_id)
            ids["items"].update(i["offer_item_id"] for i in frozen["items"] if i["offer_id"] == offer_id)
            ids["terms"].update(t["offer_id"] for t in frozen["terms"] if t["offer_id"] == offer_id)
            for key, parent in (("promotion_id", "promotions"), ("membership_plan_id", "memberships")):
                if offer.get(key) is not None:
                    ids[parent].add(offer[key])
    return ids


def render_business(business_id, repairs, frozen, plan_label=""):
    """一个业务的全部补丁渲染为单个 DO 块：冻结范围与目标行 CAS 核对，更新，写后核对。"""
    conditions = _scope_conditions(business_id)
    pks = {group: pk for group, (_, pk) in GROUP_TABLES.items()}
    statements = []
    for group, (table, pk) in GROUP_TABLES.items():
        statements.append(f"perform 1 from public.{table} t where {conditions[group]} for update;")
    for group, (table, pk) in GROUP_TABLES.items():
        statements.append(raise_if(versions_differ(table, pk, frozen[group], conditions[group]),
                                   f"stale_plan: scope_changed {table}"))
    for repair in repairs:
        pk = pks[next(g for g, (name, _) in GROUP_TABLES.items() if name == repair["table"])]
        statements.append(raise_if(f"not {row_matches(repair['table'], pk, repair['before'])}",
                                   f"stale_plan: target_changed {repair['table']} {int(repair['id'])}"))
    for repair in repairs:
        pk = pks[next(g for g, (name, _) in GROUP_TABLES.items() if name == repair["table"])]
        assignments = ", ".join(f"{column} = {sql_value(write_value(column, value))}"
                                for column, value in repair["values"].items())
        statements.append(f"update public.{repair['table']} set {assignments} where {pk} = {int(repair['id'])};")
        statements.append("get diagnostics v_count = row_count; "
                          "if v_count <> 1 then raise exception 'write_not_one_row'; end if;")
    statements.append("set constraints all immediate;")
    affected = _affected(frozen, repairs)
    for repair in repairs:
        group = next(g for g, (name, _) in GROUP_TABLES.items() if name == repair["table"])
        checks = " and ".join(f"(to_jsonb(t) -> {literal(column)}) = {jsonb_literal(value)}"
                              for column, value in repair["values"].items())
        statements.append(raise_if(
            f"not exists (select 1 from public.{repair['table']} t where t.{pks[group]} = {int(repair['id'])} and {checks})",
            f"requested_fields_changed: {repair['table']} {int(repair['id'])}"))
    for group, (table, pk) in GROUP_TABLES.items():
        ids = [row[pk] for row in frozen[group]]
        statements.append(raise_if(
            f"coalesce((select array_agg(t.{pk} order by t.{pk}) from public.{table} t where {conditions[group]}), "
            f"'{{}}'::bigint[]) is distinct from {id_array(ids)}", f"unexpected_scope_membership_change: {table}"))
        untouched = [row for row in frozen[group] if row[pk] not in affected[group]]
        statements.append(raise_if(
            versions_differ(table, pk, untouched, conditions[group], exclude=affected[group]),
            f"unexpected_row_or_trigger_change: {table}"))
    statements.append(raise_if(f"exists ({BLANKET_CHECK_SQL})", "blanket_service_mapping"))
    header = f"data quality repair {plan_label}\nbusiness_id {business_id}\nrepairs {len(repairs)}"
    return do_block(statements, declare=["v_count bigint"], header=header)


def render_audit(report, snapshot, state_dir: Path, protections: dict):
    """计划内补丁按业务渲染为 <state_dir>/pending_sql/ 下的 SQL 文件；已满足、受保护、目标缺失的补丁不渲染。"""
    frozen_scopes, by_business, blocked, skipped = {}, {}, set(), []
    for repair in report["repairs"]:
        business_id = repair["business_id"]
        if business_id in blocked:
            continue
        frozen = frozen_scopes.setdefault(business_id, scope(snapshot, business_id))
        group, pk = next((g, key) for g, (name, key) in TABLES.items() if name == repair["table"])
        target = next((r for r in frozen[group] if r[pk] == repair["id"]), None)
        if target is None:
            blocked.add(business_id)
            skipped.append({"table": repair["table"], "id": repair["id"], "business_id": business_id,
                            "reason": "target_missing"})
            continue
        if all(target.get(k) == v for k, v in repair["values"].items()):
            continue
        if _protected(frozen, repair["table"], target, protections):
            blocked.add(business_id)
            skipped.append({"table": repair["table"], "id": repair["id"], "business_id": business_id,
                            "reason": "manual_protection"})
            continue
        by_business.setdefault(business_id, []).append({**repair, "before": deepcopy(target)})
    rendered = []
    for business_id, repairs in by_business.items():
        digest = plan_digest({"business_id": business_id, "repairs": repairs})
        text = render_business(business_id, repairs, frozen_scopes[business_id], digest)
        summary = {"business_id": business_id, "repairs": [{"table": r["table"], "id": r["id"], "rule": r.get("rule"),
                                                           "values": r["values"]} for r in repairs]}
        path = write_pending(state_dir, f"data_quality_{business_id}", digest, summary, text)
        rendered.append({"business_id": business_id, "sql_path": str(path), "repairs": len(repairs)})
    return {"rendered": rendered, "skipped": skipped, "completed": not skipped}
