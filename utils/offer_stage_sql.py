from __future__ import annotations

import json
from typing import Any, Mapping

from utils.offer_stage_spec import BLANKET_CHECK_SQL, MASTER_COLUMNS, PROMOTIONS, SOURCE_SPEC
from utils.pipeline_db import do_block, expect_updated_at, literal, plan_digest


def _id_array(ids: list[int]) -> str:
    return literal(ids) + "::bigint[]"


def _raise_if(condition: str, message: str) -> str:
    return f"if {condition} then raise exception {literal(message)}; end if;"


def _expect_existing_row_updated_at(table: str, key_column: str, key: Any, updated_at: str | None, label: str) -> str:
    """核对已存在行的 updated_at 与计划一致；计划中为 None 表示该行 updated_at 为 NULL。"""
    if updated_at is not None:
        return expect_updated_at(table, key_column, key, updated_at, label)
    return _raise_if(f"(select updated_at from public.{table} where {key_column} = {literal(key)}) is not null",
                     f"stale_plan: {label}")


def _verify_fresh_statements(source: dict[str, Any]) -> list[str]:
    """核对源行 updated_at、该源下 offer 的 id 与 updated_at、各 offer 的 item id 集合与计划一致；并锁定这些行。"""
    table, spec = source["table"], SOURCE_SPEC[source["table"]]
    label = f"{table} {source['id']}"
    statements = [
        f"perform 1 from public.{table} where {spec['id']} = {literal(source['id'])} for update;",
        _expect_existing_row_updated_at(table, spec["id"], source["id"], source["source_updated_at"], f"{label} changed since planning"),
        f"perform 1 from public.promo_offer_master where {spec['offer_fk']} = {literal(source['id'])} for update;",
        _raise_if(f"coalesce((select array_agg(id order by id) from public.promo_offer_master "
                  f"where {spec['offer_fk']} = {literal(source['id'])}), '{{}}'::bigint[]) "
                  f"is distinct from {_id_array(sorted(int(i) for i in source['existing_state']))}",
                  f"stale_plan: offers of {label} changed since planning"),
    ]
    # 该源下全部 offer 的 updated_at（按 id 排序，NULL 按 NULL 比较）与全部 item 的归属各用一条语句核对
    offer_ids = sorted(int(i) for i in source["existing_state"])
    if offer_ids:
        stamps = [source["existing_state"][key] for key in sorted(source["existing_state"], key=int)]
        statements.append(_raise_if(
            f"(select array_agg(updated_at order by id) from public.promo_offer_master where id = any({_id_array(offer_ids)})) "
            f"is distinct from {literal(stamps)}::timestamptz[]",
            f"stale_plan: offers {offer_ids} of {label} changed since planning"))
    if source["existing_item_ids"]:
        pairs = sorted((int(offer_id), int(item_id)) for offer_id, items in source["existing_item_ids"].items()
                       for item_id in items)
        statements.append(_raise_if(
            f"coalesce((select array_agg(offer_id || ':' || offer_item_id order by offer_id, offer_item_id) "
            f"from public.promo_offer_items where offer_id = any({_id_array(sorted(int(i) for i in source['existing_item_ids']))})), "
            f"'{{}}'::text[]) is distinct from {literal([f'{o}:{i}' for o, i in pairs])}::text[]",
            f"stale_plan: items of the offers of {label} changed since planning"))
    return statements


def _item_statements(offer_ref: str, items: list[dict[str, Any]]) -> list[str]:
    statements = []
    for item in items:
        if item["action"] == "insert":
            statements.append(
                f"insert into public.promo_offer_items (offer_id, item_name, service_id, delivered_units, unit_price) "
                f"values ({offer_ref}, {literal(item['item_name'])}, {literal(item['service_id'])}, "
                f"{literal(item['delivered_units'])}, {literal(item['unit_price'])});")
        elif item["action"] == "update":
            statements.append(
                f"update public.promo_offer_items set item_name={literal(item['item_name'])}, "
                f"service_id={literal(item['service_id'])}, delivered_units={literal(item['delivered_units'])}, "
                f"unit_price={literal(item['unit_price'])}, updated_at=now() "
                f"where offer_item_id={literal(item['offer_item_id'])};")
        elif item["action"] == "delete":
            statements.append(f"delete from public.promo_offer_items where offer_item_id={literal(item['offer_item_id'])};")
    return statements


def _source_statements(source: dict[str, Any]) -> list[str]:
    label = f"{source['table']} {source['id']}"
    statements = _verify_fresh_statements(source)
    statements.append("v_new := '{}'::bigint[];")
    expected_true: list[int] = []
    expected_false: list[int] = []
    # 先停用旧 offer，避免与新写入的 offer 在 active 指纹唯一索引上冲突
    for action in source["offers"]:
        if action["action"] == "deactivate":
            statements.append(f"update public.promo_offer_master set is_active=false, updated_at=now() "
                              f"where id={literal(action['offer_id'])} and is_active;")
            expected_false.append(action["offer_id"])
    for action in source["offers"]:
        if action["action"] == "insert":
            master = action["master"]
            columns = ", ".join(MASTER_COLUMNS)
            values = ", ".join(literal(master[c]) for c in MASTER_COLUMNS)
            statements.append(f"insert into public.promo_offer_master ({columns}, is_active) values ({values}, false) "
                              f"returning id into v_offer;")
            statements.extend(_item_statements("v_offer", action["items"]))
            # 数据库守卫要求 per_unit offer 的每个 item 已关联本店 service；未关联时保持 inactive，等 link-services 关联
            if master["price_model"] == "per_unit" and any(item["service_id"] is None for item in action["items"]):
                continue
            statements.append("v_new := v_new || v_offer;")
            statements.append("update public.promo_offer_master set is_active=true, updated_at=now() where id=v_offer;")
        elif action["action"] == "update":
            offer_id = literal(action["offer_id"])
            patch = action["patch"]
            if patch:
                assignments = ", ".join([f"{c}={literal(v)}" for c, v in patch.items()] + ["updated_at=now()"])
                statements.append(f"update public.promo_offer_master set {assignments} where id={offer_id};")
            # 激活放在 items 写完之后，激活守卫看到的是最终 items
            statements.extend(_item_statements(offer_id, action["items"]))
            if action["reactivate"]:
                statements.append(f"update public.promo_offer_master set is_active=true, updated_at=now() where id={offer_id};")
            expected_true.append(action["offer_id"])
    mismatch = literal(f"offer_activation_changed_by_database: {label}")
    active_ids = f"(v_new || {_id_array(expected_true)})"
    statements.append(
        f"if (select count(*) from public.promo_offer_master where id = any({active_ids}) and is_active) "
        f"<> cardinality({active_ids}) then raise exception {mismatch}; end if;")
    if expected_false:
        statements.append(
            f"if (select count(*) from public.promo_offer_master where id = any({_id_array(expected_false)}) "
            f"and not is_active) <> {len(expected_false)} then raise exception {mismatch}; end if;")
    statements.append(f"v_touched := v_touched || {active_ids} || {_id_array(expected_false)};")
    return statements


def _branch_key(source: Mapping[str, Any]) -> str | None:
    """只有新建（其余 offer 不变）的来源的写入内容；连锁分店内容相同的来源键相同，可合并为一个循环写入。"""
    if any(a["action"] not in ("insert", "unchanged") for a in source["offers"]):
        return None
    shared = [{"master": {k: v for k, v in a["master"].items() if k not in ("business_id", "promotion_id",
                                                                           "membership_plan_id")},
               "items": a["items"]} for a in source["offers"] if a["action"] == "insert"]
    return json.dumps([source["table"], shared], ensure_ascii=False, sort_keys=True, default=str) if shared else None


def _branch_state(source: Mapping[str, Any]) -> dict[str, Any]:
    """一个来源计划时的状态：来源 id、商家、updated_at、名下 offer 的 id 与 updated_at（按 id 排序）、item 归属。"""
    keys = sorted(source["existing_state"], key=int)
    return {"id": source["id"], "business": source["business_id"], "updated_at": source["source_updated_at"],
            "offers": [int(k) for k in keys], "stamps": [source["existing_state"][k] for k in keys],
            "items": sorted(f"{int(o)}:{int(i)}" for o, items in source["existing_item_ids"].items() for i in items)}


def _branch_source_statements(sources: list[Mapping[str, Any]]) -> list[str]:
    """内容相同的多个来源按行循环写入，新建的 offer 与 item 内容只出现一次；循环内对每个来源同样锁定并核对
    updated_at、名下 offer 的 id 与 updated_at、item 归属与计划一致，写入后核对激活状态。"""
    table, spec = sources[0]["table"], SOURCE_SPEC[sources[0]["table"]]
    owner = "promotion_id" if table == PROMOTIONS else "membership_plan_id"
    states = literal(json.dumps([_branch_state(s) for s in sources], default=str)) + "::jsonb"
    stale = f"raise exception 'stale_plan: % % changed since planning', {literal(table)}, v_source; end if;"
    statements = [
        f"for v_row in select value from jsonb_array_elements({states}) loop",
        "v_source := (v_row->>'id')::bigint; v_business := (v_row->>'business')::bigint;",
        f"perform 1 from public.{table} where {spec['id']} = v_source for update;",
        f"if (select updated_at from public.{table} where {spec['id']} = v_source) is distinct from "
        f"(v_row->>'updated_at')::timestamptz then {stale}",
        f"perform 1 from public.promo_offer_master where {spec['offer_fk']} = v_source for update;",
        f"if coalesce((select array_agg(id order by id) from public.promo_offer_master where {spec['offer_fk']} = v_source), "
        f"'{{}}'::bigint[]) is distinct from coalesce((select array_agg(x::bigint order by n) from "
        f"jsonb_array_elements_text(v_row->'offers') with ordinality t(x, n)), '{{}}'::bigint[]) then {stale}",
        f"if coalesce((select array_agg(updated_at order by id) from public.promo_offer_master where {spec['offer_fk']} = v_source), "
        f"'{{}}'::timestamptz[]) is distinct from coalesce((select array_agg(x::timestamptz order by n) from "
        f"jsonb_array_elements_text(v_row->'stamps') with ordinality t(x, n)), '{{}}'::timestamptz[]) then {stale}",
        f"if coalesce((select array_agg(i.offer_id || ':' || i.offer_item_id order by i.offer_id || ':' || i.offer_item_id) "
        f"from public.promo_offer_items i join public.promo_offer_master m on m.id = i.offer_id "
        f"where m.{spec['offer_fk']} = v_source), '{{}}'::text[]) is distinct from coalesce((select array_agg(x order by x) "
        f"from jsonb_array_elements_text(v_row->'items') x), '{{}}'::text[]) then {stale}",
        "v_new := '{}'::bigint[];",
    ]
    columns = ", ".join(MASTER_COLUMNS)
    for action in (a for a in sources[0]["offers"] if a["action"] == "insert"):
        master = action["master"]
        values = ", ".join("v_business" if c == "business_id" else "v_source" if c == owner
                           else literal(master[c]) for c in MASTER_COLUMNS)
        statements.append(f"insert into public.promo_offer_master ({columns}, is_active) values ({values}, false) "
                          f"returning id into v_offer;")
        statements.extend(_item_statements("v_offer", action["items"]))
        # 与逐源写入相同：per_unit offer 的 item 未关联 service 时保持 inactive，等 link-services 关联
        if master["price_model"] == "per_unit" and any(item["service_id"] is None for item in action["items"]):
            continue
        statements.append("v_new := v_new || v_offer;")
        statements.append("update public.promo_offer_master set is_active=true, updated_at=now() where id=v_offer;")
    statements.append(
        "if (select count(*) from public.promo_offer_master where id = any(v_new) and is_active) <> cardinality(v_new) "
        f"then raise exception 'offer_activation_changed_by_database: % %', {literal(table)}, v_source; end if;")
    statements.append("v_touched := v_touched || v_new;")
    statements.append("end loop;")
    return statements


def render_apply(plan: Mapping[str, Any]) -> str:
    """有 offer 动作的 ready 源渲染为单个 DO 块：逐源核对未变化、先停用再写入、写后核对激活状态（is_offer_extracted 由触发器 trg_sync_is_offer_extracted 维护），
    块末对本批 offer 执行 blanket 检查；任一不符即 raise exception 整体回滚。没有 offer 动作的源无需写入。
    连锁分店内容相同的全新来源合并为一个循环写入。"""
    ready = [s for s in plan["sources"] if s["status"] == "ready" and s["offers"]]
    groups: dict[str, list[Mapping[str, Any]]] = {}
    for source in ready:
        key = _branch_key(source)
        if key is not None:
            groups.setdefault(key, []).append(source)
    grouped = {id(s) for group in groups.values() if len(group) > 1 for s in group}
    statements: list[str] = []
    for group in groups.values():
        if len(group) > 1:
            statements.extend(_branch_source_statements(group))
    for source in ready:
        if id(source) not in grouped:
            statements.extend(_source_statements(source))
    statements.append("select json_agg(t)::text into v_violations from ("
                      + BLANKET_CHECK_SQL.strip() + "\n  and m.id = any(v_touched)) t;")
    statements.append("if v_violations is not null then raise exception 'blanket_check_failed: %', v_violations; end if;")
    header = (f"extract-offers apply\nplan sha256 {plan_digest(plan)}\nsources {len(ready)} of {len(plan['sources'])}")
    return do_block(statements, declare=["v_offer bigint", "v_new bigint[]", "v_touched bigint[] := '{}'::bigint[]",
                                         "v_violations text", "v_row jsonb", "v_source bigint", "v_business bigint"],
                    header=header)
