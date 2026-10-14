"""Journaled, serializable promotion writes. No schema changes or REST writes.

The caller supplies a fresh psycopg connection and an approved frozen plan.
Receipts are completed only after COMMIT acknowledgement or exact recovery.
"""
from __future__ import annotations

from contextlib import contextmanager, suppress
from datetime import date, datetime, timezone
from decimal import Decimal, ROUND_HALF_UP
import fcntl
import hashlib
import json
import os
from pathlib import Path
import re
import tempfile
from urllib.parse import urlsplit

from psycopg import sql
from psycopg.rows import dict_row
from psycopg.types.json import Jsonb

from utils.promo_offer_items_db import is_blanket_offer_text

from utils.schema_contract import (
    CLINIC_PROMOTION_COLUMNS, OFFER_ITEM_COLUMNS, OFFER_MASTER_COLUMNS,
)

TABLES = {
    "clinic_promotions": ("promotions", "promotion_id", set(CLINIC_PROMOTION_COLUMNS) | {"is_offer_extracted"}),
    "promo_offer_master": ("offers", "id", set(OFFER_MASTER_COLUMNS) | {"is_package"}),
    "promo_offer_items": ("items", "offer_item_id", set(OFFER_ITEM_COLUMNS)),
}
BLANKET_CHECK_SQL = """
select m.id, m.offer_raw_text, i.item_name
from public.promo_offer_master m
join public.promo_offer_items i on i.offer_id = m.id
where m.is_active
  and i.service_id is not null
  and m.offer_raw_text ~* '(all services|any service|all products|all treatments|gift card|skin ?care products)'
"""
_AGENTS_BLANKET_RE = re.compile(r"all services|any service|all products|all treatments|gift card|skin ?care products", re.IGNORECASE)


def _blanket(text):
    return is_blanket_offer_text(text) or bool(_AGENTS_BLANKET_RE.search(str(text or "")))


class WriterError(ValueError):
    """Safe category only: never retain connection strings or database errors."""


def normalize_source_url(value):
    try:
        parsed = urlsplit(str(value or "").strip())
        if parsed.scheme.lower() not in {"http", "https"} or not parsed.hostname:
            raise ValueError
        if parsed.username or parsed.password:
            raise ValueError
        host = parsed.hostname.lower().removeprefix("www.")
        port = parsed.port
        suffix = f":{port}" if port and port not in {80, 443} else ""
        return host + suffix + parsed.path.rstrip("/") + ("?" + parsed.query if parsed.query else "")
    except ValueError:
        raise WriterError("invalid_source") from None


def _wire(value):
    if isinstance(value, dict):
        return {str(k): _wire(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [_wire(v) for v in value]
    if isinstance(value, Decimal):
        return int(value) if value == value.to_integral_value() else float(value)
    if isinstance(value, datetime):
        return value.astimezone(timezone.utc).isoformat() if value.tzinfo else value.isoformat()
    if isinstance(value, date):
        return value.isoformat()
    return value


def _canonical_row(row, group):
    result = _wire(dict(row))
    if group == "items":
        for alias, physical in (("id", "offer_item_id"), ("quantity", "delivered_units")):
            if alias in result:
                if physical in result and result[physical] != result[alias]:
                    raise WriterError("conflicting_alias")
                result[physical] = result.pop(alias)
    for key in ("created_at", "updated_at"):
        value = result.get(key)
        if isinstance(value, str):
            with suppress(ValueError):
                result[key] = datetime.fromisoformat(value.replace("Z", "+00:00")).isoformat()
    return result


def _graph(graph):
    return {
        group: sorted((_canonical_row(row, group) for row in graph.get(group, [])),
                      key=lambda row: row[pk])
        for group, pk in (("promotions", "promotion_id"), ("offers", "id"),
                          ("items", "offer_item_id"), ("services", "service_id"))
    }


def _rows(conn, query, params=()):
    with conn.cursor(row_factory=dict_row) as cur:
        cur.execute(query, params)
        return [_wire(row) for row in cur.fetchall()]


def read_business_graph(conn, business_id, lock=False):
    """Read full physical rows; the transaction owns snapshot consistency."""
    tail = " FOR UPDATE" if lock else ""
    promotions = _rows(conn, "SELECT * FROM public.clinic_promotions WHERE business_id = %s ORDER BY promotion_id" + tail, (business_id,))
    # Include incorrectly owned linked offers too; these must fail validation.
    offers = _rows(conn, "SELECT * FROM public.promo_offer_master WHERE business_id = %s OR promotion_id = ANY(%s) ORDER BY id" + tail,
                   (business_id, [row["promotion_id"] for row in promotions]))
    items = _rows(conn, "SELECT * FROM public.promo_offer_items WHERE offer_id = ANY(%s) ORDER BY offer_item_id" + tail,
                  ([row["id"] for row in offers],))
    services = _rows(conn, "SELECT * FROM public.clinic_services WHERE business_id = %s ORDER BY service_id" + tail, (business_id,))
    return _graph(dict(promotions=promotions, offers=offers, items=items, services=services))


def source_graph(graph, source):
    url = normalize_source_url(source["url"])
    promotions = []
    for row in graph["promotions"]:
        try:
            matches = normalize_source_url(row.get("source_url")) == url
        except WriterError:
            matches = False
        if matches:
            promotions.append(row)
    ids = {row["promotion_id"] for row in promotions}
    offers = [row for row in graph["offers"] if row.get("promotion_id") in ids]
    offer_ids = {row["id"] for row in offers}
    return _graph(dict(promotions=promotions, offers=offers,
                       items=[row for row in graph["items"] if row["offer_id"] in offer_ids],
                       services=graph["services"]))


def _ensure_directory(path):
    missing = []
    current = path
    while not current.exists():
        missing.append(current)
        current = current.parent
    for directory in reversed(missing):
        directory.mkdir(exist_ok=True)
        descriptor = os.open(directory.parent, os.O_RDONLY)
        try:
            os.fsync(descriptor)
        finally:
            os.close(descriptor)


def write_json(path, value):
    """Replace a journal durably, including directory metadata."""
    path = Path(path)
    _ensure_directory(path.parent)
    with tempfile.NamedTemporaryFile(mode="w", encoding="utf-8", dir=path.parent,
                                     prefix=".journal_", delete=False) as handle:
        temporary = Path(handle.name)
        try:
            json.dump(_wire(value), handle, sort_keys=True, ensure_ascii=False, allow_nan=False)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        except BaseException:
            temporary.unlink(missing_ok=True)
            raise
    try:
        os.replace(temporary, path)
        descriptor = os.open(path.parent, os.O_RDONLY)
        try:
            os.fsync(descriptor)
        finally:
            os.close(descriptor)
    finally:
        temporary.unlink(missing_ok=True)


@contextmanager
def _journal_lock(path):
    _ensure_directory(path.parent)
    with path.with_suffix(".lock").open("a") as handle:
        fcntl.flock(handle, fcntl.LOCK_EX)
        yield


def _receipt(journal, status, category=None):
    result = {"schema_version": 1, "status": status, "plan_id": journal["plan_id"],
              "allocated_ids": journal.get("allocated_ids", {})}
    if category:
        result["category"] = category
    if status == "completed":
        result["after"] = journal["after"]
    return result


def _finish(path, journal, status, category=None):
    journal["stage"] = status
    journal["receipt"] = _receipt(journal, status, category)
    write_json(path, journal)
    return journal["receipt"]


def _validate_plan(plan):
    if plan.get("schema_version") != 1 or plan.get("status") != "ready":
        raise WriterError("plan_not_ready")
    source = plan.get("source", {})
    if type(source.get("business_id")) is not int or source["business_id"] <= 0:
        raise WriterError("invalid_source")
    normalize_source_url(source.get("url"))
    if set(plan.get("before", {})) != {"promotions", "offers", "items", "services"}:
        raise WriterError("incomplete_before")
    operations = plan.get("operations")
    if not isinstance(operations, list):
        raise WriterError("invalid_operations")
    seen = set()
    for op in operations:
        table = op.get("table")
        if table not in TABLES or op.get("action") not in {"insert", "update"}:
            raise WriterError("invalid_operation")
        if type(op.get("id")) is not int or op["id"] == 0 or (op["id"] < 0) != (op["action"] == "insert"):
            raise WriterError("invalid_id")
        if (table, op["id"]) in seen:
            raise WriterError("duplicate_operation")
        seen.add((table, op["id"]))
        values = op.get("values")
        if not isinstance(values, dict) or not values or not set(values) <= TABLES[table][2] - {TABLES[table][1], "created_at", "updated_at", "offer_fingerprint"}:
            raise WriterError("invalid_fields")
    _graph(plan["before"])


def _allocated_absent(conn, allocated):
    for table, mapping in allocated.items():
        pk = TABLES[table][1]
        if mapping and _rows(conn, sql.SQL("SELECT {} FROM {} WHERE {} = ANY(%s)").format(
                sql.Identifier(pk), sql.Identifier("public", table), sql.Identifier(pk)),
                (list(mapping.values()),)):
            return False
    return True


def _recover(path, journal, connect):
    if journal.get("stage") == "completed":
        return journal["receipt"]
    conn = None
    try:
        conn = connect()
        conn.autocommit = True
        conn.execute("BEGIN ISOLATION LEVEL SERIALIZABLE READ ONLY")
        current = read_business_graph(conn, journal["plan"]["source"]["business_id"])
        absent = _allocated_absent(conn, journal.get("allocated_ids", {}))
        protected_current = _protected_rows(conn, _protected_refs(journal.get("protections", {})))
        if journal.get("after") is not None and current == journal["after"] and protected_current == journal.get("protected_after", {}):
            status, category = "completed", None
        elif absent and ((journal.get("before") is not None and current == journal["before"])
                         or (journal.get("before") is None and source_graph(current, journal["plan"]["source"]) == _graph(journal["plan"]["before"]))):
            status, category = "retryable", "not_committed"
        else:
            status, category = "review", "ambiguous_state"
        conn.rollback()
        return _finish(path, journal, status, category)
    except Exception:
        return _finish(path, journal, "review", "recovery_unavailable")
    finally:
        if conn is not None:
            with suppress(Exception):
                conn.close()


def recover_journal(path, connect):
    path = Path(path)
    with _journal_lock(path):
        journal = json.loads(path.read_text(encoding="utf-8"))
        return _recover(path, journal, connect)


def _allocate(conn, operations):
    allocated = {table: {} for table in TABLES}
    promotions = [op for op in operations if op["action"] == "insert" and op["table"] == "clinic_promotions"]
    if promotions:
        conn.execute("LOCK TABLE public.clinic_promotions IN SHARE ROW EXCLUSIVE MODE")
        next_id = _rows(conn, "SELECT coalesce(max(promotion_id), 0) + 1 AS next_id FROM public.clinic_promotions")[0]["next_id"]
        for op in promotions:
            allocated[op["table"]][str(op["id"])] = next_id
            next_id += 1
    for op in operations:
        table = op["table"]
        if op["action"] != "insert" or table == "clinic_promotions":
            continue
        pk = TABLES[table][1]
        sequence = _rows(conn, "SELECT pg_get_serial_sequence(%s, %s) AS sequence", ("public." + table, pk))[0]["sequence"]
        if not sequence:
            raise WriterError("missing_existing_sequence")
        # A lagging existing sequence is safe: skip occupied IDs without DDL.
        for _ in range(100):
            candidate = _rows(conn, "SELECT nextval(%s::regclass) AS next_id", (sequence,))[0]["next_id"]
            occupied = _rows(conn, sql.SQL("SELECT {} FROM {} WHERE {} = %s").format(
                sql.Identifier(pk), sql.Identifier("public", table), sql.Identifier(pk)), (candidate,))
            if not occupied:
                break
        else:
            raise WriterError("sequence_lag")
        allocated[table][str(op["id"])] = candidate
    return allocated


def _resolved(op, allocated):
    result = dict(op, values=dict(op["values"]))
    if op["id"] < 0:
        result["id"] = allocated[op["table"]][str(op["id"])]
    for key, target in (("promotion_id", "clinic_promotions"), ("offer_id", "promo_offer_master")):
        value = result["values"].get(key)
        if isinstance(value, int) and value < 0:
            try:
                result["values"][key] = allocated[target][str(value)]
            except KeyError:
                raise WriterError("unresolved_foreign_key") from None
    return result


def _mutate(conn, op, values=None):
    table = op["table"]
    pk = TABLES[table][1]
    values = dict(op["values"] if values is None else values)
    if table == "clinic_promotions" and values.get("promotion_content") is not None:
        values["promotion_content"] = Jsonb(values["promotion_content"])
    if op["action"] == "insert":
        values[pk] = op["id"]
        query = sql.SQL("INSERT INTO {} ({}) VALUES ({}) RETURNING {}").format(
            sql.Identifier("public", table), sql.SQL(", ").join(map(sql.Identifier, values)),
            sql.SQL(", ").join(sql.Placeholder() for _ in values), sql.Identifier(pk))
        params = tuple(values.values())
    else:
        query = sql.SQL("UPDATE {} SET {} WHERE {} = %s RETURNING {}").format(
            sql.Identifier("public", table), sql.SQL(", ").join(
                sql.SQL("{} = %s").format(sql.Identifier(key)) for key in values),
            sql.Identifier(pk), sql.Identifier(pk))
        params = (*values.values(), op["id"])
    if len(_rows(conn, query, params)) != 1:
        raise WriterError("write_not_one_row")


def _protected_refs(protections):
    refs = set()
    for key, table in (("offer_ids", "promo_offer_master"), ("item_ids", "promo_offer_items"), ("promotion_ids", "clinic_promotions")):
        refs.update((table, int(value)) for value in protections.get(key, []))
    for entry in protections.get("entries", []):
        if entry.get("table") not in TABLES or type(entry.get("id")) is not int:
            raise WriterError("invalid_protection")
        refs.add((entry["table"], entry["id"]))
    return refs


def _protected_rows(conn, refs, lock=False):
    result = {}
    for table, row_id in sorted(refs):
        pk = TABLES[table][1]
        rows = _rows(conn, sql.SQL("SELECT * FROM {} WHERE {} = %s" + (" FOR UPDATE" if lock else "")).format(
            sql.Identifier("public", table), sql.Identifier(pk)), (row_id,))
        if len(rows) != 1:
            raise WriterError("protected_row_missing")
        result[f"{table}:{row_id}"] = rows[0]
    promotion_ids = [row_id for table, row_id in refs if table == "clinic_promotions"]
    offer_ids = {row_id for table, row_id in refs if table == "promo_offer_master"}
    if promotion_ids:
        children = _rows(conn, "SELECT * FROM public.promo_offer_master WHERE promotion_id = ANY(%s) ORDER BY id" + (" FOR UPDATE" if lock else ""), (promotion_ids,))
        for row in children:
            result[f"promo_offer_master:{row['id']}"] = row
            offer_ids.add(row["id"])
    if offer_ids:
        children = _rows(conn, "SELECT * FROM public.promo_offer_items WHERE offer_id = ANY(%s) ORDER BY offer_item_id" + (" FOR UPDATE" if lock else ""), (sorted(offer_ids),))
        for row in children:
            result[f"promo_offer_items:{row['offer_item_id']}"] = row
    return result


def _validate_after(before, after, operations, source, today):
    before_source = source_graph(before, source)
    after_source = source_graph(after, source)
    indexes = {group: {row[pk]: row for row in after[group]} for group, pk, _ in TABLES.values()}
    expected_sets = {group: {row[pk] for row in before_source[group]} for group, pk, _ in TABLES.values()}
    for op in operations:
        group, pk, _ = TABLES[op["table"]]
        if op["action"] == "insert":
            expected_sets[group].add(op["id"])
        row = indexes[group].get(op["id"])
        expected = dict(op["values"])
        if row is not None and group == "offers":
            regular, discount = row.get("regular_price"), row.get("discount_price")
            if regular is not None and discount is not None and Decimal(str(regular)) > 0 and Decimal(str(discount)) >= 0 and Decimal(str(discount)) < Decimal(str(regular)):
                difference = Decimal(str(regular)) - Decimal(str(discount))
                expected["discount_amount"] = _wire(difference.quantize(Decimal(".01"), rounding=ROUND_HALF_UP))
                expected["discount_percent"] = _wire((difference * 100 / Decimal(str(regular))).quantize(Decimal(".01"), rounding=ROUND_HALF_UP))
        if row is None or any(row.get(key) != _wire(value) for key, value in expected.items()):
            raise WriterError("requested_fields_changed")
    for group, pk, _ in TABLES.values():
        if {row[pk] for row in after_source[group]} != expected_sets[group]:
            raise WriterError("source_membership_changed")
    # Updates may not reparent rows, even within the source graph.
    for group, pk, _ in TABLES.values():
        for row in before_source[group]:
            current = indexes[group][row[pk]]
            for key in ("business_id", "promotion_id", "offer_id", "membership_plan_id"):
                if current.get(key) != row.get(key):
                    raise WriterError("ownership_changed")
            active_ops = [op for op in operations if TABLES[op["table"]][0] == group and op["id"] == row[pk]]
            requested_active = next((op["values"]["is_active"] for op in active_ops if "is_active" in op["values"]), row.get("is_active"))
            if "is_active" in row and current.get("is_active") != requested_active:
                raise WriterError("unexpected_deactivation")
    promotions = {row["promotion_id"]: row for row in after_source["promotions"]}
    services = {row["service_id"]: row for row in after["services"]}
    for promotion in promotions.values():
        if promotion.get("business_id") != source["business_id"]:
            raise WriterError("cross_business_source")
    for offer in after_source["offers"]:
        blanket = _blanket(str(offer.get("offer_raw_text") or ""))
        if offer.get("business_id") != source["business_id"] or offer.get("membership_plan_id") is not None:
            raise WriterError("invalid_offer_source")
        # Historical inactive rows can already be incomplete. Preserve their
        # identity/ownership/protections; their old defects must not block a
        # current offer update or a deliberate deactivation.
        if not offer.get("is_active"):
            continue
        items = [row for row in after_source["items"] if row["offer_id"] == offer["id"]]
        if not items:
            raise WriterError("offer_without_items")
        seen = set()
        for item in items:
            qty = item.get("delivered_units")
            if qty is not None and (isinstance(qty, bool) or Decimal(str(qty)) <= 0):
                raise WriterError("invalid_quantity")
            if offer.get("is_package") and item.get("unit_price") is not None:
                raise WriterError("package_unit_price")
            service = services.get(item.get("service_id"))
            if item.get("service_id") is None and not str(item.get("item_name") or "").strip():
                raise WriterError("missing_item_name")
            if blanket and item.get("service_id") is not None:
                raise WriterError("blanket_service_mapping")
            if item.get("service_id") is not None and (not service or service.get("business_id") != source["business_id"]):
                raise WriterError("cross_business_mapping")
            if not blanket and not offer.get("is_package") and offer.get("discount_price") is not None:
                unit = item.get("unit_price")
                if unit is None or (Decimal(str(unit)) * Decimal(str(qty if qty is not None else 1))).quantize(Decimal(".01"), rounding=ROUND_HALF_UP) != Decimal(str(offer["discount_price"])).quantize(Decimal(".01"), rounding=ROUND_HALF_UP):
                    raise WriterError("unit_total_mismatch")
            if offer.get("is_active"):
                key = ("service", item["service_id"]) if item.get("service_id") is not None else ("name", str(item.get("item_name") or "").strip().casefold())
                if not blanket and key in seen:
                    raise WriterError("duplicate_service")
                seen.add(key)
        if offer.get("is_active"):
            promotion = promotions[offer["promotion_id"]]
            if not promotion.get("is_active") or (promotion.get("campaign_end_date") and date.fromisoformat(str(promotion["campaign_end_date"])[:10]) < today):
                raise WriterError("inactive_promotion")


def apply_plan(plan, connect, journal_dir: Path, protections: dict):
    """Apply one ready plan; retryable receipts permit an explicit retry."""
    _validate_plan(plan)
    payload = json.dumps(_wire(plan), sort_keys=True, separators=(",", ":"), allow_nan=False)
    plan_id = hashlib.sha256(payload.encode()).hexdigest()
    path = Path(journal_dir) / f"{plan_id}.json"
    with _journal_lock(path):
        if path.exists():
            journal = json.loads(path.read_text(encoding="utf-8"))
            receipt = _recover(path, journal, connect)
            if receipt["status"] != "retryable":
                return receipt
            if _protected_refs(journal.get("protections", {})) != _protected_refs(protections):
                return _finish(path, journal, "review", "protection_changed")
        else:
            journal = {"schema_version": 1, "plan_id": plan_id, "plan": _wire(plan),
                       "protections": _wire(protections), "stage": "prepared", "allocated_ids": {}}
            write_json(path, journal)
        conn = None
        committing = False
        try:
            conn = connect()
            conn.autocommit = True
            conn.execute("BEGIN ISOLATION LEVEL SERIALIZABLE")
            # Lock before taking the snapshot: promotion IDs use max+1 without DDL.
            if any(op["table"] == "clinic_promotions" and op["action"] == "insert" for op in plan["operations"]):
                conn.execute("LOCK TABLE public.clinic_promotions IN SHARE ROW EXCLUSIVE MODE")
            before = read_business_graph(conn, plan["source"]["business_id"], lock=True)
            if source_graph(before, plan["source"]) != _graph(plan["before"]):
                raise WriterError("stale_before")
            if plan["source"].get("promotion_id") is not None and plan["source"]["promotion_id"] not in {row["promotion_id"] for row in source_graph(before, plan["source"])["promotions"]}:
                raise WriterError("source_id_mismatch")
            refs = _protected_refs(protections)
            protected = _protected_rows(conn, refs, lock=True)
            expanded_refs = {(key.rsplit(":", 1)[0], int(key.rsplit(":", 1)[1])) for key in protected}
            source_before = source_graph(before, plan["source"])
            for op in plan["operations"]:
                group, pk, _ = TABLES[op["table"]]
                if op["action"] == "update" and op["id"] not in {row[pk] for row in source_before[group]}:
                    raise WriterError("operation_outside_source")
                if (op["table"], op["id"]) in expanded_refs:
                    raise WriterError("protected_operation")
                for field, parent in (("promotion_id", "clinic_promotions"), ("offer_id", "promo_offer_master")):
                    if (parent, op["values"].get(field)) in expanded_refs:
                        raise WriterError("protected_operation")
            allocated = _allocate(conn, plan["operations"])
            journal.update(before=before, protected_before=protected, allocated_ids=allocated, stage="allocated")
            journal.pop("after", None)
            write_json(path, journal)
            operations = [_resolved(op, allocated) for op in plan["operations"]]
            activations = []
            for table in TABLES:
                for op in operations:
                    if op["table"] != table:
                        continue
                    values = dict(op["values"])
                    if table == "promo_offer_master" and (values.get("is_active") is True or op["action"] == "insert"):
                        requested = values.get("is_active", False)
                        values["is_active"] = False
                        activations.append(dict(op, action="update", values={"is_active": requested}))
                    _mutate(conn, op, values)
            for op in activations:
                _mutate(conn, op)
            conn.execute("SET CONSTRAINTS ALL IMMEDIATE")
            after = read_business_graph(conn, plan["source"]["business_id"], lock=True)
            if _protected_rows(conn, refs) != protected:
                raise WriterError("protected_row_changed")
            today = _rows(conn, "SELECT current_date AS today")[0]["today"]
            _validate_after(before, after, operations, plan["source"], date.fromisoformat(str(today)))
            if _rows(conn, BLANKET_CHECK_SQL):
                raise WriterError("blanket_service_mapping")
            journal.update(after=after, protected_after=protected, stage="committing")
            write_json(path, journal)
            committing = True
            conn.commit()
            committing = False
            return _finish(path, journal, "completed")
        except Exception as exc:
            if conn is not None:
                with suppress(Exception):
                    conn.rollback()
                with suppress(Exception):
                    conn.close()
                conn = None
            if committing:
                return _recover(path, journal, connect)
            category = str(exc) if isinstance(exc, WriterError) else "database_error"
            return _finish(path, journal, "review", category)
        finally:
            if conn is not None:
                with suppress(Exception):
                    conn.close()
