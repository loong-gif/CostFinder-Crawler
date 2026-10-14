"""Frozen PATCH-only quality repairs with CAS, journals and trigger-aware readback."""
from __future__ import annotations

from copy import deepcopy
from datetime import datetime, timedelta, timezone
from pathlib import Path

from utils.data_quality_audit import TABLES, _protected
from utils.monitor_price_rest_writer import _cas, _transition, _graph, _blanket_check
from utils.monitor_price_store import atomic_json, now, read_json
from utils.monitor_price_writer import WriterError


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
    result["confirmations"] = fetch_all(client, "offer_confirmations", "id")
    result["audit"] = fetch_all(client, "admin_audit_log", "id")
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


def read_scope(client, business_id):
    result = {group: fetch_all(client, TABLES[group][0], TABLES[group][1],
                              filters={"business_id": f"eq.{int(business_id)}"})
              for group in ("promotions", "memberships", "services", "offers")}
    for group, field in (("promotions", "promotion_id"), ("memberships", "membership_plan_id")):
        pk = TABLES[group][1]
        ids = [r[pk] for r in result[group]]
        for start in range(0, len(ids), 150):
            linked = fetch_all(client, "promo_offer_master", "id", filters={field: "in.(" + ",".join(map(str, ids[start:start+150])) + ")"})
            result["offers"] = list({r["id"]: r for r in result["offers"] + linked}.values())
    ids = [r["id"] for r in result["offers"]]
    result["items"], result["terms"] = [], []
    for start in range(0, len(ids), 150):
        filters = {"offer_id": "in.(" + ",".join(map(str, ids[start:start+150])) + ")"}
        result["items"].extend(fetch_all(client, "promo_offer_items", "offer_item_id", filters=filters))
        result["terms"].extend(fetch_all(client, "promo_offer_terms", "offer_id", filters=filters))
    return result


def _canonical(snapshot):
    return {**_graph(snapshot), "memberships": sorted(snapshot.get("memberships", []), key=lambda r: r["plan_id"])}


def verify_transition(before, after, repair):
    table = repair["table"]
    if table in {"clinic_promotions", "promo_offer_master", "promo_offer_items"}:
        _transition(_graph(before), _graph(after), {"table": table, "id": repair["id"],
                                                "action": "update", "values": repair["values"]})
        if before.get("memberships") != after.get("memberships"):
            raise WriterError("unexpected_membership_change")
        return
    # Compare copies: the after scope is persisted as the next repair's frozen state.
    expected, actual = _canonical(deepcopy(before)), _canonical(deepcopy(after))
    group, pk = next((g, key) for g, (name, key) in TABLES.items() if name == table)
    wanted = next((r for r in expected[group] if r[pk] == repair["id"]), None)
    found = next((r for r in actual[group] if r[pk] == repair["id"]), None)
    if wanted is None or found is None:
        raise WriterError("row_missing_or_ambiguous")
    wanted.update(repair["values"])
    wanted.pop("updated_at", None)
    found.pop("updated_at", None)
    if expected != actual:
        raise WriterError("unexpected_row_or_trigger_change")


def _receipt(path, journal, status, reason=None):
    journal["status"] = status
    receipt = {"table": journal["repair"]["table"], "id": journal["repair"]["id"],
               "status": status, "atomic": False, "journal": str(path), "verified_at": now()}
    if reason:
        receipt["reason"] = reason
    if status == "review" and journal.get("request_sent"):
        receipt["resume_required"] = True
    journal["receipt"] = receipt
    atomic_json(path, journal)
    return receipt


def _protected_current(client, current, repair, prior):
    from utils.monitor_price_store import manual_protections
    current = dict(current)
    current["confirmations"] = fetch_all(client, "offer_confirmations", "id")
    current["audit"] = fetch_all(client, "admin_audit_log", "id")
    protections = manual_protections(current, prior)
    return _protected(current, repair["table"], repair["before"], protections)


def apply_repair(repair, frozen_scope, client, directory: Path, protections: dict):
    client.assert_write_project()
    # The caller shares the monitor lock. Rows are still compared before every request.
    name = f"{repair['table']}_{int(repair['id'])}_{datetime.now(timezone.utc):%Y%m%d_%H%M%S_%f}.json"
    path = directory / name
    journal = {"prepared_at": now(), "repair": deepcopy(repair), "before": deepcopy(frozen_scope),
               "request_sent": False, "status": "prepared"}
    atomic_json(path, journal)
    return _execute(path, journal, client, protections)


def _execute(path, journal, client, protections):
    repair, before = journal["repair"], journal["before"]
    try:
        current = read_scope(client, repair["business_id"])
        if _protected_current(client, current, repair, protections):
            return _receipt(path, journal, "review", "manual_protection")
        if journal.get("request_sent"):
            try:
                verify_transition(before, current, repair)
                journal["after"] = current
                return _receipt(path, journal, "completed")
            except WriterError:
                if _canonical(before) != _canonical(current):
                    return _receipt(path, journal, "review", "unknown_or_conflicting_result")
        if _canonical(before) != _canonical(current):
            return _receipt(path, journal, "conflict", "scope_changed")
        prepared = datetime.fromisoformat(journal["prepared_at"].replace("Z", "+00:00"))
        if datetime.now(timezone.utc) - prepared > timedelta(hours=24):
            return _receipt(path, journal, "review", "frozen_plan_expired")
        group, pk = next((g, key) for g, (name, key) in TABLES.items() if name == repair["table"])
        row = next((r for r in current[group] if r[pk] == repair["id"]), None)
        if row != repair["before"]:
            return _receipt(path, journal, "conflict", "target_changed")
        journal.update(request_sent=True, status="sent")
        atomic_json(path, journal)
        returned = client.update_row(repair["table"], _cas(row, repair["values"], pk), repair["values"])
        journal["returned"] = returned
        atomic_json(path, journal)
        if len(returned) != 1:
            return _receipt(path, journal, "conflict", "cas_miss")
        after = read_scope(client, repair["business_id"])
        verify_transition(before, after, repair)
        journal["after"] = after
        return _receipt(path, journal, "completed")
    except Exception as exc:
        # Network responses and connection strings must not leak into the receipt.
        reason = str(exc) if isinstance(exc, WriterError) else type(exc).__name__
        return _receipt(path, journal, "review", reason)


def recover_repairs(client, directory: Path, protections: dict):
    receipts = []
    for path in sorted(directory.glob("*.json")):
        journal = read_json(path)
        if journal.get("status") in {"prepared", "sent", "review"}:
            receipts.append(_execute(path, journal, client, protections))
    return receipts


def apply_audit(report, snapshot, client, directory: Path, protections: dict):
    receipts = recover_repairs(client, directory, protections)
    blocked = set()
    for receipt in receipts:
        if receipt.get("resume_required"):
            blocked.add(read_json(Path(receipt["journal"]))["repair"]["business_id"])
    frozen = {}
    for repair in report["repairs"]:
        business_id = repair["business_id"]
        if business_id in blocked:
            continue
        expected = frozen.setdefault(business_id, scope(snapshot, business_id))
        group, pk = next((g, key) for g, (name, key) in TABLES.items() if name == repair["table"])
        target = next((r for r in expected[group] if r[pk] == repair["id"]), None)
        if target is None:
            blocked.add(business_id)
            continue
        if all(target.get(k) == v for k, v in repair["values"].items()):
            continue
        # Advance only over our own independently verified changes, never a new DB snapshot.
        adjusted = {**repair, "before": deepcopy(target)}
        receipt = apply_repair(adjusted, expected, client, directory, protections)
        receipts.append(receipt)
        if receipt["status"] == "completed":
            frozen[business_id] = read_json(Path(receipt["journal"]))["after"]
        else:
            blocked.add(business_id)
    try:
        _blanket_check(client)
        blanket_verified = True
    except Exception:
        blanket_verified = False
    return {"receipts": receipts, "blanket_verified": blanket_verified,
            "completed": blanket_verified and all(r["status"] == "completed" for r in receipts)}
