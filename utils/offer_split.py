"""把一条 offer 里并列的多个服务拆成各自独立的 offer（非 package）。

只处理一组价格折扣覆盖多个服务的情况：原文里出现的数值都属于这条 offer 已存的价格字段，字段原样复制到每个服务。
原文含分号的组合行、含不同价格的并列都不处理。每个服务一条新 offer、一个 item，原 offer 停用。
"""
from __future__ import annotations

import json
import re
from decimal import Decimal
from typing import Any, Mapping

from utils.offer_scope_filter import item_name_problem
from utils.pipeline_db import do_block, literal, plan_digest, render
from utils.promo_offer_items_db import is_blanket_offer_text
from utils.service_linking import scan_services

OFFER_COLUMNS = ("id, business_id, promotion_id, membership_plan_id, offer_raw_text, price_model, regular_price, "
                 "discount_price, discount_percent, discount_amount, is_membership_required, is_new_customer_required, "
                 "conditions, is_package, updated_at")
ITEM_COLUMNS = "offer_item_id, offer_id, item_name, service_id, delivered_units, unit_price"
OFFER_WHERE = "is_active and not is_package and offer_raw_text not like '%;%'"
PRICE_FIELDS = ("regular_price", "discount_price", "discount_percent", "discount_amount")
MONEY_RE = re.compile(r"\$\s*([\d,]+(?:\.\d+)?)|(\d+(?:\.\d+)?)\s*(?:%|percent)", re.IGNORECASE)


def snapshot_tables() -> dict[str, tuple[str, str, str]]:
    return {
        "offers": ("promo_offer_master", OFFER_COLUMNS, OFFER_WHERE),
        "items": ("promo_offer_items", ITEM_COLUMNS,
                  f"offer_id in (select id from public.promo_offer_master where {OFFER_WHERE})"),
    }


def _numbers(text: str) -> set[Decimal]:
    return {Decimal((match.group(1) or match.group(2)).replace(",", "")).normalize() for match in MONEY_RE.finditer(text)}


def _stored_numbers(offer: Mapping[str, Any]) -> set[Decimal]:
    return {Decimal(str(offer[field])).normalize() for field in PRICE_FIELDS if offer[field] is not None}


COMMON_WORDS = frozenset(
    "a an all and any are as at beyond book buy by cosmetic day days each eligible every first for from get have in "
    "including is it just member members month months new of off on only or our per percent plus price "
    "pricing receive reg regular regularly round save savings select special specials the then to unit units up when "
    "with year yearly your injectable injectables inject toxin toxins tox neurotoxin neurotoxins treatment treatments "
    "service services".split())
BRANCH_RE = re.compile(r"^([^|]+?) \| ")
LIST_SEPARATOR_RE = re.compile(r"[,&/+·()]|\b(?:and|or)\b", re.IGNORECASE)


def _unrecognized_words(raw: str, services: set[str]) -> set[str]:
    """原文里既不属于已识别服务名、也不是通用词的单词；非空说明并列清单里还有词典没有覆盖的服务。"""
    covered = {word.casefold() for service in services for word in re.findall(r"[A-Za-z0-9]+", service)}
    words = {word.casefold() for word in re.findall(r"[A-Za-z][A-Za-z0-9]*", raw)}
    return {word for word in words if word not in covered and word not in COMMON_WORDS and len(word) > 1}


def _skip_reason(offer: Mapping[str, Any], services: set[str]) -> str:
    raw = offer["offer_raw_text"]
    if is_blanket_offer_text(raw):
        return "blanket_offer"
    branch = BRANCH_RE.match(raw)
    if branch and scan_services(branch.group(1)):
        return "already_independent_branch"
    if len(services) < 2:
        return "single_or_generic_service"
    if any(len(scan_services(term)) > 1 for term in LIST_SEPARATOR_RE.split(raw)):
        return "services_not_listed"
    if not _numbers(raw) <= _stored_numbers(offer):
        return "text_has_other_prices"
    if _unrecognized_words(raw, services):
        return "unrecognized_words_in_list"
    return ""


def build_plan(snapshot: Mapping[str, list[dict[str, Any]]]) -> dict[str, Any]:
    items_by_offer: dict[int, list[dict[str, Any]]] = {}
    for item in snapshot["items"]:
        items_by_offer.setdefault(item["offer_id"], []).append(item)
    splits: list[dict[str, Any]] = []
    skipped: list[dict[str, Any]] = []
    for offer in snapshot["offers"]:
        services = scan_services(offer["offer_raw_text"])
        reason = _skip_reason(offer, services)
        if reason:
            if len(services) >= 2:
                skipped.append({"offer_id": offer["id"], "reason": reason, "services": sorted(services)})
            continue
        old_items = items_by_offer.get(offer["id"], [])
        pieces = []
        for service in sorted(services):
            same = [item for item in old_items if scan_services(item["item_name"]) == {service}]
            old = same[0] if len(same) == 1 else None
            unit_price = old["unit_price"] if old is not None else None
            if unit_price is None and offer["price_model"] == "per_unit":
                unit_price = offer["discount_price"]
            piece = {
                "offer_raw_text": f"{service} | {offer['offer_raw_text']}",
                "item_name": service,
                "service_id": old["service_id"] if old is not None else None,
                "delivered_units": old["delivered_units"] if old is not None else None,
                "unit_price": unit_price,
            }
            if item_name_problem(piece["item_name"]):
                raise RuntimeError(f"invalid_service_label:{service}")
            piece["activate"] = not (offer["price_model"] == "per_unit" and piece["service_id"] is None)
            pieces.append(piece)
        splits.append({"offer": {key: (value.isoformat() if key == "updated_at" else value)
                                 for key, value in offer.items()},
                       "pieces": pieces})
    return {"splits": splits, "skipped": skipped}


def _json(rows: list[Any], *, as_pairs: bool = False) -> str:
    """JSON 数组字面量；as_pairs 时把 [id, updated_at] 对转成 {id, updated_at} 对象。"""
    if as_pairs:
        rows = [{"id": row[0], "updated_at": row[1]} for row in rows]
    return literal(json.dumps(rows, ensure_ascii=False, separators=(",", ":"), default=str)) + "::jsonb"


def render_apply(plan: Mapping[str, Any]) -> str:
    """逐条核对原 offer 仍是计划记录的状态；每个服务插入一条独立 offer（先 inactive，item 写完再激活；与已有
    active offer 指纹相同则跳过该条），最后停用原 offer。"""
    splits = plan["splits"]
    checks = [[split["offer"]["id"], split["offer"]["updated_at"]] for split in splits]
    rows = [{key: value for key, value in {
        "offer_id": split["offer"]["id"], "service": piece["item_name"], "service_id": piece["service_id"],
        "delivered_units": piece["delivered_units"], "unit_price": piece["unit_price"],
        "activate": piece["activate"]}.items() if value is not None}
        for split in splits for piece in split["pieces"]]
    statements = [
        "select count(*) into v_count from jsonb_to_recordset(" + _json(checks, as_pairs=True) + ") as x(id bigint, "
        "updated_at timestamptz) join public.promo_offer_master m on m.id = x.id and m.is_active "
        "and m.updated_at = x.updated_at;",
        f"if v_count <> {len(checks)} then raise exception 'stale_plan: some offers changed since planning'; end if;",
        "for r in select * from jsonb_to_recordset(" + _json(rows) + ") as x(offer_id bigint, service text, "
        "service_id bigint, delivered_units numeric, unit_price numeric, activate boolean) loop",
        "begin",
        "insert into public.promo_offer_master (business_id, promotion_id, membership_plan_id, "
        "is_new_customer_required, is_membership_required, conditions, offer_raw_text, price_model, regular_price, "
        "discount_price, discount_percent, discount_amount, is_package, is_active) select business_id, promotion_id, "
        "membership_plan_id, is_new_customer_required, is_membership_required, conditions, "
        "r.service || ' | ' || offer_raw_text, price_model, regular_price, discount_price, discount_percent, "
        "discount_amount, false, false from public.promo_offer_master where id = r.offer_id returning id into v_offer;",
        "insert into public.promo_offer_items (offer_id, item_name, service_id, delivered_units, unit_price) "
        "values (v_offer, r.service, r.service_id, r.delivered_units, r.unit_price);",
        "if r.activate then update public.promo_offer_master set is_active = true, updated_at = now() "
        "where id = v_offer; end if;",
        "exception when unique_violation then null;",
        "end;",
        "end loop;",
        "update public.promo_offer_master set is_active = false, updated_at = now() where id = any("
        f"{literal([split['offer']['id'] for split in plan['splits']])}::bigint[]) and is_active;",
    ]
    header = (f"split-offers apply\nplan sha256 {plan_digest(plan)}\noffers {len(splits)}, new offers {len(rows)}")
    return do_block(statements, declare=["v_offer bigint", "v_count bigint", "r record"], header=header)
