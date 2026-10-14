"""Recoverable service-key DML using existing REST tables; never atomic or DDL.

The shared REST client encodes params and returns representations. Local journal
locking serializes this monitor only: CAS and readbacks detect other writers but
cannot lock a parent together with trigger-modified children across requests.
"""
from __future__ import annotations

from copy import deepcopy
from datetime import date, datetime, timezone, timedelta
from decimal import Decimal, ROUND_HALF_UP
import hashlib
import json
from pathlib import Path

from requests import HTTPError
from utils.monitor_image_gate import image_write_block

from utils.monitor_price_writer import (
    TABLES as BASE_TABLES, WriterError, _AGENTS_BLANKET_RE, _graph as base_graph, _journal_lock,
    _protected_refs, _resolved as base_resolved, _validate_after, _validate_plan, _wire,
    source_graph as base_source_graph, write_json,
)

TERMS = "promo_offer_terms"
SERVICE_COLUMNS = "service_id,business_id,service_name,service_area,source_url"
TABLES = {**BASE_TABLES, TERMS: ("terms", "offer_id", {
    "offer_id", "membership_plan_id", "min_quantity", "min_unit", "promo_code", "raw_terms",
    "is_new_customer_required", "is_returning_customer_required", "is_membership_required",
    "redemption_frequency", "requires_consultation", "is_not_combinable", "is_one_per_patient",
})}
_LEGACY_TERMS_FIELDS = {"is_membership_required", "membership_plan_id", "is_new_customer_required", "conditions"}


def _all(client, table, filters=None, select="*"):
    rows, offset = [], 0
    pk = TABLES[table][1] if table in TABLES else "service_id"
    while True:
        page = client.fetch_rows(table, select, filters=filters, limit=500,
                                 offset=offset, order=f"{pk}.asc")
        rows.extend(page)
        if len(page) < 500:
            return rows
        offset += len(page)


def _in(ids):
    return "in.(" + ",".join(str(int(i)) for i in sorted(ids)) + ")"


def _graph(data):
    graph = base_graph(data)
    graph["terms"] = sorted((_wire(dict(row)) for row in data.get("terms", [])),
                            key=lambda row: row["offer_id"])
    return graph


def source_graph(graph, source):
    result = base_source_graph(graph, source)
    offer_ids = {row["id"] for row in result["offers"]}
    result["terms"] = [row for row in graph.get("terms", []) if row.get("offer_id") in offer_ids]
    return _graph(result)


def read_business_graph(client, business_id):
    promotions = _all(client, "clinic_promotions", {"business_id": f"eq.{int(business_id)}"})
    offers = _all(client, "promo_offer_master", {"business_id": f"eq.{int(business_id)}"})
    if promotions:
        linked = _all(client, "promo_offer_master", {"promotion_id": _in(r["promotion_id"] for r in promotions)})
        offers = list({r["id"]: r for r in offers + linked}.values())
    items = _all(client, "promo_offer_items", {"offer_id": _in(r["id"] for r in offers)}) if offers else []
    services = _all(client, "clinic_services", {"business_id": f"eq.{int(business_id)}"},
                    SERVICE_COLUMNS)
    terms = _all(client, TERMS, {"offer_id": _in(r["id"] for r in offers)}) if offers else []
    return _graph(dict(promotions=promotions, offers=offers, items=items, services=services, terms=terms))


def _row(graph, table, row_id):
    group, pk, _ = TABLES[table]
    matches = [r for r in graph[group] if r[pk] == row_id]
    if len(matches) != 1:
        raise WriterError("row_missing_or_ambiguous")
    return matches[0]


def _resolved(op, allocated):
    if op["table"] != TERMS:
        return base_resolved(op, allocated)
    result = deepcopy(op)
    parent_id = result["values"].get("offer_id", result["id"])
    if isinstance(parent_id, int) and parent_id < 0:
        try:
            parent_id = allocated["promo_offer_master"][str(parent_id)]
        except KeyError:
            raise WriterError("unresolved_terms_offer") from None
    result["id"] = parent_id
    if "offer_id" in result["values"]:
        result["values"]["offer_id"] = parent_id
    return result


def _protect(graph, operations, protections):
    """Block both directions of parent/child trigger effects before any DML."""
    refs = _protected_refs(protections)
    offers = {r["id"]: r for r in graph["offers"]}
    items = {r["offer_item_id"]: r for r in graph["items"]}
    for op in operations:
        table, row_id = op["table"], op["id"]
        touches = {(table, row_id)}
        if table == "clinic_promotions":
            # Live trigger inventory: this flag fires no child/cascade trigger.
            children = ([r for r in offers.values() if r.get("promotion_id") == row_id]
                        if set(op["values"]) - {"is_offer_extracted"} else [])
        elif table == "promo_offer_master":
            parent = op["values"].get("promotion_id", offers.get(row_id, {}).get("promotion_id"))
            touches.add(("clinic_promotions", parent))
            children = [offers.get(row_id, {"id": row_id})]
        else:
            parent = row_id if table == TERMS else op["values"].get("offer_id", items.get(row_id, {}).get("offer_id"))
            children = [offers.get(parent, {"id": parent})]
            touches.add(("clinic_promotions", offers.get(parent, {}).get("promotion_id")))
        for child in children:
            touches.add(("promo_offer_master", child["id"]))
            touches.update(("promo_offer_items", i["offer_item_id"]) for i in items.values() if i["offer_id"] == child["id"])
        if touches & refs:
            raise WriterError("protected_row_or_cascade")


def _protection_operations(operations, allocated):
    """Resolve already-created records without demanding future insert IDs."""
    result = deepcopy(operations)
    for op in result:
        table = "promo_offer_master" if op["table"] == TERMS else op["table"]
        if op["id"] < 0:
            op["id"] = allocated.get(table, {}).get(str(op["id"]), op["id"])
        for field, parent in (("promotion_id", "clinic_promotions"), ("offer_id", "promo_offer_master")):
            value = op["values"].get(field)
            if isinstance(value, int) and value < 0:
                op["values"][field] = allocated.get(parent, {}).get(str(value), value)
    return result


def _auto_prices(row, old=None):
    """Mirror live fn_auto_fix_offer_prices, including stale derived clearing."""
    regular, discount = row.get("regular_price"), row.get("discount_price")
    if regular is not None and discount is not None and Decimal(str(regular)) > 0 and 0 <= Decimal(str(discount)) < Decimal(str(regular)):
        difference = Decimal(str(regular)) - Decimal(str(discount))
        row["discount_amount"] = _round(difference, ".01")
        row["discount_percent"] = _round(difference * 100 / Decimal(str(regular)), ".01")
        return
    old = old or {}
    regular, discount = old.get("regular_price"), old.get("discount_price")
    if regular is not None and discount is not None and Decimal(str(regular)) > 0 and 0 <= Decimal(str(discount)) < Decimal(str(regular)):
        difference = Decimal(str(regular)) - Decimal(str(discount))
        derived = {"discount_amount": _round(difference, ".01"),
                   "discount_percent": _round(difference * 100 / Decimal(str(regular)), ".01")}
        for field, value in derived.items():
            if row.get(field) == old.get(field) == value:
                row[field] = None


def _restore_prices(old, values):
    desired = {**old, **values}
    actual = deepcopy(desired)
    _auto_prices(actual, old)
    return {key: desired[key] for key in ("discount_amount", "discount_percent")
            if desired.get(key) is not None and actual.get(key) is None}


def _validate_terms_operations(plan):
    """Terms use the Offer PK, including inserts for existing positive IDs."""
    offers = {r["id"] for r in plan["before"].get("offers", [])}
    offers.update(op["id"] for op in plan["operations"]
                  if op["table"] == "promo_offer_master" and op["action"] == "insert")
    existing = {r["offer_id"] for r in plan["before"].get("terms", [])}
    seen = set()
    for op in plan["operations"]:
        if op.get("table") != TERMS:
            continue
        row_id, values = op.get("id"), op.get("values")
        if op.get("action") not in {"insert", "update"}:
            raise WriterError("invalid_terms_action")
        if type(row_id) is not int or row_id == 0 or row_id not in offers:
            raise WriterError("invalid_terms_reference")
        if row_id in seen:
            raise WriterError("duplicate_operation")
        seen.add(row_id)
        if not isinstance(values, dict) or not values or set(values) - TABLES[TERMS][2]:
            raise WriterError("invalid_terms_operation")
        if "offer_id" in values and (type(values["offer_id"]) is not int or values["offer_id"] != row_id):
            raise WriterError("invalid_terms_operation")
        if values.get("membership_plan_id") is not None:
            raise WriterError("ownership_changed")
        if op["action"] == "insert":
            if values.get("offer_id") != row_id or row_id in existing:
                raise WriterError("invalid_terms_reference")
        elif row_id not in existing or row_id < 0:
            raise WriterError("invalid_terms_reference")


def _sync_legacy_terms(graph, offer):
    """Mirror trg_sync_offer_terms_from_legacy_fields and its nullable defaults."""
    raw = offer.get("conditions") or []
    returning = any("returning customer required" in str(term).lower() for term in raw)
    new = offer.get("is_new_customer_required")
    values = {"offer_id": offer["id"], "raw_terms": deepcopy(raw),
              "membership_plan_id": offer.get("membership_plan_id"),
              "is_new_customer_required": True if new is True else False if returning else new,
              "is_returning_customer_required": False if new is True else True if returning else None,
              "is_membership_required": True if offer.get("membership_plan_id") is not None else offer.get("is_membership_required")}
    row = next((r for r in graph["terms"] if r["offer_id"] == offer["id"]), None)
    if row is None:
        row = dict.fromkeys(TABLES[TERMS][2])
        graph["terms"].append(row)
    row.update(values)


def _check_terms(before, expected, actual, touched):
    old_ids = {r["offer_id"] for r in before.get("terms", [])}
    wanted, found = deepcopy(expected.get("terms", [])), deepcopy(actual.get("terms", []))
    for rows in (wanted, found):
        for row in rows:
            if row["offer_id"] in touched:
                row.pop("updated_at", None)
                if row["offer_id"] not in old_ids:
                    row.pop("created_at", None)
    if sorted(wanted, key=lambda r: r["offer_id"]) != sorted(found, key=lambda r: r["offer_id"]):
        raise WriterError("unexpected_terms_or_trigger_change")


def _validate_rest_after(before, after, operations, source):
    _validate_after(base_graph(before), base_graph(after),
                    [op for op in operations if op["table"] != TERMS], source, date.today())
    expected, touched = deepcopy(before), set()
    for op in operations:
        if op["table"] == "promo_offer_master" and (op["action"] == "insert" or set(op["values"]) & _LEGACY_TERMS_FIELDS):
            _sync_legacy_terms(expected, _row(after, op["table"], op["id"]))
            touched.add(op["id"])
    for op in operations:
        if op["table"] != TERMS:
            continue
        row = next((r for r in expected["terms"] if r["offer_id"] == op["id"]), None)
        if row is None:
            row = dict.fromkeys(TABLES[TERMS][2])
            row["offer_id"] = op["id"]
            row["raw_terms"] = []
            expected["terms"].append(row)
        row.update(_wire(op["values"]))
        touched.add(op["id"])
    _check_terms(before, expected, after, touched)


def _preflight(plan):
    """Validate the final graph and detect known unrepresentable REST stages."""
    before = _graph(plan["before"])
    final = deepcopy(before)
    for op in plan["operations"]:
        group, pk, _ = TABLES[op["table"]]
        if op["action"] == "insert":
            row = next((r for r in final[group] if r[pk] == op["id"]), None) if op["table"] == TERMS else None
            if row is None:
                row = dict.fromkeys(TABLES[TERMS][2]) if op["table"] == TERMS else {}
                if op["table"] == TERMS:
                    row["raw_terms"] = []
                row.update({pk: op["id"], **deepcopy(op["values"])})
                final[group].append(row)
            else:
                row.update(op["values"])
        else:
            row = _row(final, op["table"], op["id"])
            if op["table"] == "promo_offer_master" and row.get("is_package") and op["values"].get("is_package") is False:
                discount = op["values"].get("discount_price", row.get("discount_price"))
                fires = any(k in op["values"] and op["values"][k] != row.get(k) for k in ("discount_price", "price_model"))
                if discount is not None and not fires and any(
                    i.get("unit_price") is None or _round(Decimal(str(i["unit_price"])) * Decimal(str(i.get("delivered_units") or 1)), ".01") != _round(discount, ".01")
                    for i in final["items"] if i["offer_id"] == row["id"]):
                    raise WriterError("rest_intermediate_price_state")
            row.update(op["values"])
        if op["table"] == "promo_offer_master" and (op["action"] == "insert" or set(op["values"]) & _LEGACY_TERMS_FIELDS):
            _sync_legacy_terms(final, row)
    # Master totals can update unchanged items through the live sync trigger.
    for op in plan["operations"]:
        if op["table"] == "promo_offer_master" and set(op["values"]) & {"discount_price", "price_model"}:
            offer = _row(final, op["table"], op["id"])
            if not offer.get("is_package") and offer.get("discount_price") is not None:
                for item in final["items"]:
                    if item["offer_id"] == offer["id"]:
                        item["unit_price"] = _round(Decimal(str(offer["discount_price"])) / Decimal(str(item.get("delivered_units") or 1)))
    for offer in final["offers"]:
        regular, discount = offer.get("regular_price"), offer.get("discount_price")
        if regular is not None and discount is not None and 0 <= Decimal(str(discount)) < Decimal(str(regular)):
            difference = Decimal(str(regular)) - Decimal(str(discount))
            offer["discount_amount"] = _round(difference, ".01")
            offer["discount_percent"] = _round(difference * 100 / Decimal(str(regular)), ".01")
    _validate_rest_after(before, final, plan["operations"], plan["source"])


def _steps(plan):
    graph, operations = _graph(plan["before"]), plan["operations"]
    affected = set()
    for op in operations:
        if op["table"] == "promo_offer_master" and op["action"] == "update":
            affected.add(op["id"])
        elif op["table"] in {"promo_offer_items", TERMS}:
            parent = op["id"] if op["table"] == TERMS else op["values"].get("offer_id")
            if parent is None:
                parent = _row(graph, op["table"], op["id"])["offer_id"]
            if parent > 0:
                affected.add(parent)
        elif (op["table"] == "clinic_promotions" and op["action"] == "update"
              and set(op["values"]) - {"is_offer_extracted"}):
            affected.update(r["id"] for r in graph["offers"] if r["promotion_id"] == op["id"])
    steps = []
    for row_id in sorted(affected):
        if _row(graph, "promo_offer_master", row_id).get("is_active"):
            steps.append(dict(table="promo_offer_master", action="update", id=row_id, values={"is_active": False}))
    touched_promotions = {r["promotion_id"] for r in graph["offers"] if r["id"] in affected}
    completion_flags = {}
    for row in graph["promotions"]:
        if row["promotion_id"] in touched_promotions and row.get("is_offer_extracted") is True:
            completion_flags[row["promotion_id"]] = True
            steps.append(dict(table="clinic_promotions", action="update", id=row["promotion_id"], values={"is_offer_extracted": False}))
    # Promotion flags stay incomplete until all child requests have verified.
    for table in TABLES:
        for original in operations:
            if original["table"] != table:
                continue
            op = deepcopy(original)
            if table == "clinic_promotions":
                op["values"].pop("is_active", None)
                op["values"]["is_offer_extracted"] = False
                if op["action"] == "insert":
                    op["values"]["is_active"] = False
            elif table == "promo_offer_master":
                op["values"]["is_active"] = False
            steps.append(op)
            if table == "promo_offer_master" and op["action"] == "update":
                restore = _restore_prices(_row(graph, table, op["id"]), op["values"])
                if restore:
                    steps.append(dict(table=table, action="update", id=op["id"], values=restore))
    for op in operations:
        if op["table"] == "clinic_promotions" and op["values"].get("is_active") is True:
            steps.append(dict(table=op["table"], action="update", id=op["id"], values={"is_active": True}))
    desired_active = {r["id"]: r.get("is_active", False) for r in graph["offers"] if r["id"] in affected}
    for op in operations:
        if op["table"] == "promo_offer_master":
            desired_active[op["id"]] = op["values"].get("is_active", desired_active.get(op["id"], False))
    for row_id, active in sorted(desired_active.items()):
        if active:
            steps.append(dict(table="promo_offer_master", action="update", id=row_id, values={"is_active": True}))
    for op in operations:
        if op["table"] == "clinic_promotions":
            values = {k: v for k, v in op["values"].items() if k in {"is_active", "is_offer_extracted"}}
            if "is_offer_extracted" not in values and op["action"] == "update":
                values["is_offer_extracted"] = _row(graph, op["table"], op["id"]).get("is_offer_extracted", False)
            if values:
                steps.append(dict(table=op["table"], action="update", id=op["id"], values=values))
    planned_promotions = {op["id"] for op in operations if op["table"] == "clinic_promotions"}
    for row_id, flag in completion_flags.items():
        if row_id not in planned_promotions:
            steps.append(dict(table="clinic_promotions", action="update", id=row_id, values={"is_offer_extracted": flag}))
    return steps


def _eq(value, column=None):
    if value is None:
        return "is.null"
    if isinstance(value, bool):
        return "is." + str(value).lower()
    if isinstance(value, (list, dict)):
        # Both columns are text[]; quote each PostgreSQL array element.
        if isinstance(value, list) and column in {"conditions", "raw_terms"}:
            raw = "{" + ",".join('"' + str(v).replace("\\", "\\\\").replace('"', '\\"') + '"' for v in value) + "}"
        else:
            raw = json.dumps(value, separators=(",", ":"), ensure_ascii=False)
    else:
        raw = str(value)
    # requests handles URL encoding. PostgREST casts the raw filter operand;
    # outer JSON quotes would become literal quotes in numeric/timestamp casts.
    return "eq." + raw


def _cas(row, values, pk):
    # Guard timestamp, ownership, old payload values and active state. JSON and
    # array values remain encoded params, never hand-concatenated into a URL.
    keys = set(values) | {pk, "updated_at", "business_id", "promotion_id", "membership_plan_id", "offer_id", "is_active"}
    return {k: _eq(row[k], k) for k in sorted(keys) if k in row}


def _round(value, scale=".0001"):
    return _wire(Decimal(str(value)).quantize(Decimal(scale), rounding=ROUND_HALF_UP))


def _transition(before, after, op, returned=None):
    """Accept only requested changes plus known existing trigger consequences.

    Generated timestamps/fingerprints may change only on rows touched by this
    request or its trigger closure. All other rows/fields must match exactly.
    """
    if op["table"] == TERMS:
        expected = deepcopy(before)
        if op["action"] == "insert":
            if returned is None or len(returned) != 1:
                raise WriterError("insert_result_missing")
            row = deepcopy(returned[0])
            if any(row.get(k) != _wire(v) for k, v in op["values"].items()):
                raise WriterError("requested_fields_changed")
            expected["terms"].append(row)
        else:
            row = _row(expected, TERMS, op["id"])
            row.update(op["values"])
        actual = _graph(after)
        _check_terms(before, expected, actual, {op["id"]})
        if base_graph(expected) != base_graph(actual):
            raise WriterError("unexpected_terms_or_trigger_change")
        return
    expected = deepcopy(before)
    table, row_id, values = op["table"], op["id"], op["values"]
    group, pk, _ = TABLES[table]
    if op["action"] == "insert":
        if returned is None or len(returned) != 1:
            raise WriterError("insert_result_missing")
        row = deepcopy(returned[0])
        requested = {**row, **values}
        if table == "promo_offer_master":
            _auto_prices(requested)
        if any(row.get(k) != requested.get(k) for k in values):
            raise WriterError("requested_fields_changed")
        expected[group].append(row)
        row_id = row[pk]
    else:
        row = _row(expected, table, row_id)
        row.update(values)
    affected = {(table, row_id)}
    if table == "clinic_promotions" and values.get("is_active") is False:
        for offer in expected["offers"]:
            if offer["promotion_id"] == row_id and offer.get("is_active") is True:
                offer["is_active"] = False
                affected.add(("promo_offer_master", offer["id"]))
    if table in {"promo_offer_master", "promo_offer_items"}:
        offer_id = row_id if table == "promo_offer_master" else row["offer_id"]
        offer = _row(expected, "promo_offer_master", offer_id)
        parent_trigger = table == "promo_offer_master" or op["action"] == "insert" or bool(set(values) & {"offer_id", "service_id", "delivered_units", "item_name"})
        if parent_trigger:
            affected.add(("promo_offer_master", offer_id))
        children = [i for i in expected["items"] if i["offer_id"] == offer_id]
        if table == "promo_offer_master":
            old = _row(before, table, row_id) if op["action"] == "update" else {}
            sync = not offer.get("is_package") and any(
                key in values and values[key] != old.get(key) for key in ("discount_price", "price_model"))
            normalized = children if sync else []
        else:
            normalize_current = op["action"] == "insert" or bool(set(values) & {"offer_id", "service_id", "delivered_units", "unit_price"})
            sync_all = op["action"] == "insert" or bool(set(values) & {"offer_id", "service_id", "delivered_units"})
            clear_package = offer.get("is_package") and (op["action"] == "insert" or bool(set(values) & {"offer_id", "service_id"}))
            normalized = children if ((sync_all and not offer.get("is_package")) or clear_package) else ([row] if normalize_current else [])
        for item in normalized:
            old_unit = item.get("unit_price")
            if offer.get("is_package") or (offer.get("discount_price") is None and offer.get("price_model") != "per_unit"):
                item["unit_price"] = None
            elif offer.get("discount_price") is not None:
                item["unit_price"] = _round(Decimal(str(offer["discount_price"])) / Decimal(str(item.get("delivered_units") or 1)))
            if item.get("unit_price") != old_unit:
                affected.add(("promo_offer_items", item["offer_item_id"]))
        old_offer = next((r for r in before["offers"] if r["id"] == offer_id), {})
        if parent_trigger:
            _auto_prices(offer, old_offer)
    actual = deepcopy(after)
    terms_touched = set()
    if table == "promo_offer_master" and (op["action"] == "insert" or set(values) & _LEGACY_TERMS_FIELDS):
        _sync_legacy_terms(expected, row)
        terms_touched.add(row_id)
    _check_terms(before, expected, actual, terms_touched)
    for table_name, ident in affected:
        group_name, key, _ = TABLES[table_name]
        wanted = next((r for r in expected[group_name] if r[key] == ident), None)
        found = next((r for r in actual[group_name] if r[key] == ident), None)
        if wanted is None or found is None:
            raise WriterError("source_membership_changed")
        for metadata in ("updated_at", "offer_fingerprint"):
            wanted.pop(metadata, None)
            found.pop(metadata, None)
    if base_graph(expected) != base_graph(actual):
        raise WriterError("unexpected_row_or_trigger_change")


def _blanket_check(client):
    active = _all(client, "promo_offer_master", {"is_active": "is.true"}, "id,offer_raw_text")
    ids = [r["id"] for r in active if _AGENTS_BLANKET_RE.search(str(r.get("offer_raw_text") or ""))]
    for offset in range(0, len(ids), 200):
        if _all(client, "promo_offer_items", {"offer_id": _in(ids[offset:offset+200]), "service_id": "not.is.null"}):
            raise WriterError("blanket_service_mapping")


def _finish(path, journal, status, category=None):
    journal["stage"] = status
    receipt = {"schema_version": 1, "transport": "rest", "plan_id": journal["plan_id"],
               "status": status, "allocated_ids": journal["allocated_ids"],
               "verified_steps": journal.get("next_step", 0), "atomic": False}
    if category:
        receipt["category"] = category
    if status not in {"completed", "superseded"}:
        receipt["resume_required"] = True
    elif status == "completed":
        receipt["after"] = journal["current"]
    journal["receipt"] = receipt
    write_json(path, journal)
    return receipt


def _expired_without_requests(journal):
    prepared = journal["plan"].get("prepared_at")
    if not prepared:
        return False
    expired = datetime.now(timezone.utc) - datetime.fromisoformat(prepared.replace("Z", "+00:00")) > timedelta(hours=24)
    # Missing current proves the initial GET never succeeded. For newer journals,
    # the counter is durably advanced BEFORE each request. A cleared pending PATCH
    # or a zero next_step alone does not prove that no request was sent.
    unstarted = "current" not in journal or journal.get("requests_sent") == 0
    return expired and unstarted and not journal.get("pending")


def _review_readback(path, journal, client):
    # An unresolved insert stays unresolved; reading never authorizes another POST.
    receipt = dict(journal["receipt"])
    try:
        source = journal["plan"]["source"]
        current = source_graph(read_business_graph(client, source["business_id"]), source)
        journal["review_readback"] = {"observed_at": datetime.now(timezone.utc).isoformat(),
                                     "current": current, "matches_last_ack": current == journal.get("current")}
        receipt["readback"] = {k: v for k, v in journal["review_readback"].items() if k != "current"}
        write_json(path, journal)
    except Exception:
        receipt["readback"] = {"status": "unavailable"}
    return receipt


def _reconcile(path, journal, client):
    current = source_graph(read_business_graph(client, journal["plan"]["source"]["business_id"]), journal["plan"]["source"])
    if "current" not in journal:
        if current != _graph(journal["plan"]["before"]):
            raise WriterError("stale_before")
        return _finish(path, journal, "retryable", "no_request_sent")
    pending = journal.get("pending")
    if pending:
        if pending["op"]["action"] == "insert":
            if pending.get("returned"):
                _transition(journal["current"], current, pending["op"], pending["returned"])
                journal["current"] = current
                journal["next_step"] += 1
                journal.pop("pending")
                write_json(path, journal)
                return _finish(path, journal, "retryable", "verified_resume")
            # No idempotency token/unique constraint exists for default IDs.
            # Candidate readback is useful for review, never authorizes repost.
            group, pk, _ = TABLES[pending["op"]["table"]]
            old_ids = {r[pk] for r in journal["current"][group]}
            journal["insert_candidates"] = [r for r in current[group] if r[pk] not in old_ids]
            return _finish(path, journal, "review", "insert_outcome_unknown")
        if current == journal["current"]:
            journal.pop("pending")
        else:
            _transition(journal["current"], current, pending["op"])
            journal["current"] = current
            journal["next_step"] += 1
            journal.pop("pending")
        write_json(path, journal)
    elif current != journal["current"]:
        raise WriterError("external_change_conflict")
    return _finish(path, journal, "retryable", "verified_resume")


def recover_journal(path, client):
    path = Path(path)
    with _journal_lock(path):
        journal = json.loads(path.read_text())
        if journal.get("transport") != "rest":
            return {"status": "review", "category": "legacy_sql_journal_requires_review"}
        if journal.get("stage") in {"completed", "superseded"}:
            return journal["receipt"]
        if _expired_without_requests(journal):
            return _finish(path, journal, "superseded", "expired_unstarted_refresh_evidence")
        if journal.get("category"):
            return _review_readback(path, journal, client)
        try:
            return _reconcile(path, journal, client)
        except Exception as exc:
            category = str(exc) if isinstance(exc, WriterError) else "readback_unavailable"
            if isinstance(exc, WriterError):
                journal["category"] = category
            return _finish(path, journal, "review", category)


def apply_plan(plan, client, journal_dir: Path, protections: dict):
    base_plan = {**plan, "before": base_graph(plan["before"]),
                 "operations": [op for op in plan["operations"] if op["table"] != TERMS]}
    _validate_plan(base_plan)
    if any(op.get("table") not in TABLES for op in plan.get("operations", [])):
        raise WriterError("unknown_table")
    _validate_terms_operations(plan)
    payload = json.dumps(_wire(plan), sort_keys=True, separators=(",", ":"), allow_nan=False)
    plan_id = hashlib.sha256(payload.encode()).hexdigest()
    path = Path(journal_dir) / f"{plan_id}.json"
    with _journal_lock(path):
        if path.exists():
            journal = json.loads(path.read_text())
            if journal.get("transport") != "rest":
                return {"status": "review", "category": "legacy_sql_journal_requires_review"}
            if journal.get("stage") in {"completed", "superseded"}:
                return journal["receipt"]
            if journal.get("category"):
                return _review_readback(path, journal, client)
        else:
            journal = {"schema_version": 1, "transport": "rest", "plan_id": plan_id,
                       "plan": _wire(plan), "protections": _wire(protections),
                       "allocated_ids": {table: {} for table in TABLES}, "next_step": 0,
                       "requests_sent": 0}
        image_block = image_write_block(plan, journal_dir)
        if image_block:
            # Readback recovery stays available; a policy hold is not a journal failure.
            return {"status": "review", "category": image_block,
                    "resume_required": bool(journal.get("requests_sent")), "journal_path": str(path)}
        if _expired_without_requests(journal):
            return _finish(path, journal, "superseded", "expired_unstarted_refresh_evidence")
        try:
            current = source_graph(read_business_graph(client, plan["source"]["business_id"]), plan["source"])
            if "current" not in journal:
                if current != _graph(plan["before"]):
                    raise WriterError("stale_before")
                _protect(current, plan["operations"], protections)
                _preflight(plan)
                for op in plan["operations"]:
                    if op["action"] == "update":
                        old = _row(current, op["table"], op["id"])
                        for key in ("business_id", "promotion_id", "offer_id", "membership_plan_id"):
                            if key in op["values"] and op["values"][key] != old.get(key):
                                raise WriterError("ownership_changed")
                journal.update(current=current, before=current, steps=_steps(plan), stage="prepared")
                write_json(path, journal)
            else:
                receipt = _reconcile(path, journal, client)
                if receipt["status"] != "retryable":
                    return receipt
                _protect(journal["current"], _protection_operations(plan["operations"], journal["allocated_ids"]), protections)
            while journal["next_step"] < len(journal["steps"]):
                image_block = image_write_block(plan, journal_dir)
                if image_block:
                    return {"status": "review", "category": image_block,
                            "resume_required": True, "journal_path": str(path)}
                if plan.get("prepared_at"):
                    prepared = datetime.fromisoformat(plan["prepared_at"].replace("Z", "+00:00"))
                    if datetime.now(timezone.utc) - prepared > timedelta(hours=24):
                        raise WriterError("evidence_expired_requires_review")
                logical = journal["steps"][journal["next_step"]]
                allocated = journal["allocated_ids"]
                if logical["table"] == TERMS:
                    op = _resolved(logical, allocated)
                    # The verified legacy-field trigger can create this row
                    # before its planned insert. Update it with CAS, not upsert.
                    if op["action"] == "insert" and any(r["offer_id"] == op["id"] for r in journal["current"]["terms"]):
                        op["action"] = "update"
                elif logical["action"] == "insert":
                    op = deepcopy(logical)
                    for field, parent in (("promotion_id", "clinic_promotions"), ("offer_id", "promo_offer_master")):
                        value = op["values"].get(field)
                        if isinstance(value, int) and value < 0:
                            op["values"][field] = allocated[parent][str(value)]
                    if op["table"] == "clinic_promotions":
                        rows = client.fetch_rows("clinic_promotions", "promotion_id", limit=1, order="promotion_id.desc")
                        op["values"]["promotion_id"] = (rows[0]["promotion_id"] if rows else 0) + 1
                else:
                    op = _resolved(logical, allocated)
                # Fresh source membership and versions are checked before every
                # request; the PATCH itself additionally checks the old row.
                fresh = source_graph(read_business_graph(client, plan["source"]["business_id"]), plan["source"])
                if fresh != journal["current"]:
                    raise WriterError("external_change_conflict")
                _protect(fresh, _protection_operations(plan["operations"], allocated), protections)
                journal["pending"] = {"op": op, "logical_id": logical["id"]}
                # Preserve uncertainty even if a later old-state read clears pending.
                journal["requests_sent"] = journal.get("requests_sent", 1) + 1
                write_json(path, journal)
                if op["action"] == "insert":
                    returned = client.insert_rows(op["table"], [op["values"]])
                else:
                    pk = TABLES[op["table"]][1]
                    row = _row(fresh, op["table"], op["id"])
                    returned = client.update_row(op["table"], _cas(row, op["values"], pk), op["values"])
                    if len(returned) != 1:
                        raise WriterError("cas_conflict")
                # Persist acknowledgement/IDs before any further network I/O.
                # A later GET timeout must not discard a known sequence ID.
                if op["action"] == "insert":
                    pk = TABLES[op["table"]][1]
                    if len(returned) != 1 or type(returned[0].get(pk)) is not int or returned[0][pk] <= 0:
                        raise WriterError("insert_result_missing")
                    allocated[op["table"]][str(logical["id"])] = returned[0][pk]
                journal["pending"]["returned"] = _wire(returned)
                write_json(path, journal)
                after = source_graph(read_business_graph(client, plan["source"]["business_id"]), plan["source"])
                _transition(fresh, after, op, returned)
                journal.update(current=after, next_step=journal["next_step"] + 1)
                journal.pop("pending")
                write_json(path, journal)
            operations = [_resolved(op, journal["allocated_ids"]) for op in plan["operations"]]
            _validate_rest_after(journal["before"], journal["current"], operations, plan["source"])
            _blanket_check(client)
            return _finish(path, journal, "completed")
        except Exception as exc:
            category = str(exc) if isinstance(exc, WriterError) else "request_or_readback_unconfirmed"
            if isinstance(exc, HTTPError) and exc.response is not None:
                category = "request_rejected"
                journal["category"] = category
            elif isinstance(exc, WriterError):
                journal["category"] = category
            # No rollback exists. Keep pending request and all verified steps.
            return _finish(path, journal, "review", category)
