"""Frozen, one-off Supabase MCP application; no schema or monitor changes."""
from __future__ import annotations

import csv
import json
import sys
from collections import Counter
from copy import deepcopy
from decimal import Decimal, ROUND_HALF_UP
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from utils.monitor_price_store import atomic_json, manual_protections, now
from utils.monitor_price_writer_plan import normalize_source_url

OUT = ROOT / "output/monitor_price_sync/apply_20261011"
BASE = ROOT / "output/monitor_price_sync/dryrun_20261010"
TABLES = {
    "promotions": ("clinic_promotions", "promotion_id"),
    "offers": ("promo_offer_master", "id"),
    "items": ("promo_offer_items", "offer_item_id"),
    "terms": ("promo_offer_terms", "offer_id"),
    "services": ("clinic_services", "service_id"),
    "history": ("promo_offer_price_history", "id"),
    "queue": ("promotion_content_guardrail_queue", "id"),
}


def load(path):
    return json.loads(Path(path).read_text())


def literal(value):
    # The connector accepts raw SQL, not bound arguments. Data is a typed JSON
    # payload; this encoder prevents any input from escaping its SQL literal.
    if not isinstance(value, str):
        raise TypeError("SQL literal must be text")
    if "\x00" in value:
        raise ValueError("NUL cannot be represented in PostgreSQL text")
    return "E'" + value.replace("\\", "\\\\").replace("'", "''") + "'"


def ids(values):
    values = sorted(set(values))
    if any(type(v) is not int or v <= 0 for v in values):
        raise ValueError("positive integer identifiers required")
    return "ARRAY[" + ",".join(map(str, values)) + "]::bigint[]"


def snapshot_sql(business_ids, *, auxiliary=False):
    b = ids(business_ids)
    ctes = f"""b AS (SELECT business_id,name,website FROM public.master_business_info WHERE business_id=ANY({b})),
p AS (SELECT * FROM public.clinic_promotions WHERE business_id=ANY({b})),
m AS (SELECT * FROM public.promo_offer_master WHERE business_id=ANY({b}) OR promotion_id IN (SELECT promotion_id FROM p)),
i AS (SELECT * FROM public.promo_offer_items WHERE offer_id IN (SELECT id FROM m)),
t AS (SELECT * FROM public.promo_offer_terms WHERE offer_id IN (SELECT id FROM m)),
s AS (SELECT service_id,business_id,service_name,service_area,unit_type,source_url FROM public.clinic_services WHERE business_id=ANY({b})),
c AS (SELECT id,offer_id,business_id,kind,status,created_at,resolved_at FROM public.offer_confirmations WHERE offer_id IN (SELECT id FROM m)),
a AS (SELECT id,action,target_type,target_id,created_at FROM public.admin_audit_log
 WHERE (target_type IN ('deal','offer') AND target_id IN (SELECT id::text FROM m))
 OR (target_type='promotion' AND target_id IN (SELECT promotion_id::text FROM p))
 OR (target_type='item' AND target_id IN (SELECT offer_item_id::text FROM i)))"""
    arrays = {"businesses": ("b", "business_id"), "promotions": ("p", "promotion_id"),
              "offers": ("m", "id"), "items": ("i", "offer_item_id"), "terms": ("t", "offer_id"),
              "services": ("s", "service_id"), "confirmations": ("c", "id"), "audit": ("a", "id")}
    if auxiliary:
        ctes += ",h AS (SELECT * FROM public.promo_offer_price_history WHERE offer_id IN (SELECT id FROM m)),q AS (SELECT * FROM public.promotion_content_guardrail_queue WHERE promotion_id IN (SELECT promotion_id FROM p))"
        arrays.update(history=("h", "id"), queue=("q", "id"))
    parts = ["'read_at',now()", "'project_id','kdlpkjzcnbkjcvwsvlwn'"]
    parts += [f"'{key}',(SELECT coalesce(jsonb_agg(to_jsonb(x) ORDER BY {pk}), '[]'::jsonb) FROM {table} x)" for key, (table, pk) in arrays.items()]
    return "BEGIN TRANSACTION ISOLATION LEVEL SERIALIZABLE READ ONLY; WITH " + ctes + " SELECT jsonb_build_object(" + ",".join(parts) + ") AS snapshot; ROLLBACK;\n"


def bootstrap():
    OUT.mkdir(parents=True, exist_ok=True)
    evidence = load(BASE / "dryrun_evidence.json")
    bids = sorted({p["business_id"] for p in evidence["page_coverage"] if p["business_id"]})
    packets = []
    for index in range(0, len(bids), 6):
        name = f"read_{index//6:02d}"
        (OUT / f"{name}.sql").write_text(snapshot_sql(bids[index:index+6]))
        packets.append({"name": name, "business_ids": bids[index:index+6]})
    atomic_json(OUT / "read_manifest.json", {"started_at": now(), "packets": packets, "pages": 189})


def combine():
    manifest = load(OUT / "read_manifest.json")
    merged = {k: [] for k in ["businesses", "promotions", "offers", "items", "terms", "services", "confirmations", "audit"]}
    reads = []
    for packet in manifest["packets"]:
        data = load(OUT / f'{packet["name"]}.json')
        reads.append({"file": packet["name"] + ".json", "read_at": data["read_at"], "business_ids": packet["business_ids"]})
        for key in merged:
            merged[key].extend(data[key])
    for key in merged:
        pk = TABLES.get(key, (None, "business_id" if key == "businesses" else "id"))[1]
        unique = {}
        for row in merged[key]:
            if row[pk] in unique and unique[row[pk]] != row:
                raise ValueError(f"concurrent snapshot conflict: {key} {row[pk]}")
            unique[row[pk]] = row
        merged[key] = list(unique.values())
    merged.update(read_at=now(), read_log=reads, project_id="kdlpkjzcnbkjcvwsvlwn")
    atomic_json(OUT / "database_before.json", merged)
    previous = load(BASE / "dryrun_evidence.json")["manual_protections"]
    atomic_json(OUT / "manual_protections.json", manual_protections(merged, previous))
    print(json.dumps({key: len(value) for key, value in merged.items() if isinstance(value, list)}))


ALLOWED = {
    "promotions": {"promotion_title", "promotion_content", "campaign_end_date", "is_active", "is_offer_extracted"},
    "offers": {"business_id", "promotion_id", "membership_plan_id", "offer_raw_text", "regular_price", "discount_price", "discount_amount", "discount_percent", "is_package", "price_model", "conditions", "is_active", "is_new_customer_required", "is_membership_required", "id"},
    "items": {"offer_item_id", "offer_id", "item_name", "service_id", "delivered_units", "unit_price"},
    "terms": {"is_one_per_patient", "min_quantity", "min_unit"},
}
PRICE_FIELDS = ["regular_price", "discount_price", "discount_amount", "discount_percent"]


def rounded(value, places="0.01"):
    return float(Decimal(str(value)).quantize(Decimal(places), rounding=ROUND_HALF_UP))


def graph_keys(snapshot):
    return {key: snapshot[key] for key in ["businesses", "promotions", "offers", "items", "terms", "services", "confirmations", "audit", "history", "queue"]}


def simulate(before, operations):
    """Expected business state, with only real trigger effects represented."""
    expected = deepcopy(graph_keys(before))
    changed = {key: set() for key in TABLES}
    expected_history = []
    def lookup(key, row_id):
        return next(r for r in expected[key] if r[TABLES[key][1]] == row_id)
    for op in operations:
        key, row_id, values = op["table"], op["id"], op["values"]
        if key not in ALLOWED or not set(values) <= ALLOWED[key]:
            raise ValueError("non-allowlisted operation")
        if type(row_id) is not int or row_id <= 0:
            raise ValueError("invalid operation ID")
        if op.get("insert"):
            row = deepcopy(values)
            expected[key].append(row)
            old = None
        else:
            row = lookup(key, row_id)
            old = deepcopy(row)
            row.update(values)
        changed[key].add(row_id)
        if key == "promotions" and row.get("is_active") is False:
            for offer in expected["offers"]:
                if offer["promotion_id"] == row_id and offer["is_active"]:
                    offer["is_active"] = False
                    changed["offers"].add(offer["id"])
        if key == "offers":
            regular, sale = row.get("regular_price"), row.get("discount_price")
            if regular is not None and sale is not None and regular > 0 and regular >= sale:
                row["discount_amount"] = rounded(Decimal(str(regular)) - Decimal(str(sale)))
                row["discount_percent"] = rounded((Decimal(str(regular)) - Decimal(str(sale))) / Decimal(str(regular)) * 100)
            if old and any(field in values for field in PRICE_FIELDS) and any(old.get(f) != row.get(f) for f in PRICE_FIELDS):
                expected_history.append({"offer_id": row_id, **{f"{which}_{f}": source.get(f) for which, source in [("old", old), ("new", row)] for f in PRICE_FIELDS}})
            if op.get("insert") or {"conditions", "is_new_customer_required", "is_membership_required", "membership_plan_id"} & values.keys():
                term = next((r for r in expected["terms"] if r["offer_id"] == row_id), None)
                returning = any("returning customer required" in c.lower() for c in row.get("conditions") or [])
                synced = {"offer_id": row_id, "raw_terms": row.get("conditions") or [],
                          "is_new_customer_required": True if row.get("is_new_customer_required") else False if returning else row.get("is_new_customer_required"),
                          "is_returning_customer_required": False if row.get("is_new_customer_required") else True if returning else None,
                          "is_membership_required": True if row.get("membership_plan_id") is not None else row.get("is_membership_required"),
                          "membership_plan_id": row.get("membership_plan_id")}
                if term is None:
                    expected["terms"].append(synced)
                else:
                    term.update(synced)
                changed["terms"].add(row_id)
            if {"discount_price", "price_model"} & values.keys() and not row.get("is_package"):
                for item in expected["items"]:
                    if item["offer_id"] == row_id:
                        item["unit_price"] = None if sale is None else rounded(Decimal(str(sale)) / Decimal(str(item.get("delivered_units") or 1)), "0.0001")
                        changed["items"].add(item["offer_item_id"])
        if key == "items":
            master = lookup("offers", row["offer_id"])
            if op.get("insert") or {"offer_id", "service_id", "delivered_units", "unit_price"} & values.keys():
                if master["is_package"]:
                    row["unit_price"] = None
                elif master.get("discount_price") is not None:
                    row["unit_price"] = rounded(Decimal(str(master["discount_price"])) / Decimal(str(row.get("delivered_units") or 1)), "0.0001")
            if op.get("insert") or {"offer_id", "service_id", "delivered_units", "item_name"} & values.keys():
                changed["offers"].add(row["offer_id"])
            if master["is_package"] and (op.get("insert") or {"offer_id", "service_id", "delivered_units"} & values.keys()):
                for sibling in expected["items"]:
                    if sibling["offer_id"] == row["offer_id"]:
                        sibling["unit_price"] = None
                        changed["items"].add(sibling["offer_item_id"])
    for key, row_ids in changed.items():
        for row in expected[key]:
            if row[TABLES[key][1]] in row_ids:
                row.pop("updated_at", None)
                if key == "offers":
                    row.pop("offer_fingerprint", None)
    return expected, {k: sorted(v) for k, v in changed.items()}, expected_history


def make_plan(promotion_id, changes, *, new=None):
    before = load(OUT / f"aux_{promotion_id}.json")
    source = next(p for p in before["promotions"] if p["promotion_id"] == promotion_id)
    by_offer = {m["id"]: m for m in before["offers"]}
    operations = []
    # Parent validity must be corrected before final activation of child offers.
    operations += [c for c in changes if c["table"] == "promotions"]
    master_changes = [c for c in changes if c["table"] == "offers"]
    for op in master_changes:
        if op["values"].get("is_active") is not False:
            op = deepcopy(op)
            op["values"]["is_active"] = False
        operations.append(op)
    operations += [c for c in changes if c["table"] in {"items", "terms"}]
    if new:
        operations += new
    for op in master_changes:
        if op["values"].get("is_active") is not False:
            operations.append({"table": "offers", "id": op["id"], "values": {"is_active": by_offer[op["id"]]["is_active"]}})
    if new:
        operations += [{"table": "offers", "id": c["id"], "values": {"is_active": True}} for c in new if c["table"] == "offers"]
    expected, changed, history = simulate(before, operations)
    protections = load(OUT / "manual_protections.json")
    for entry in protections["entries"]:
        for key, (table, _) in TABLES.items():
            if entry["table"] == table and entry["id"] in changed[key]:
                raise ValueError(f"protected mutation: {table}/{entry['id']}")
    touched_offers = set(changed["offers"])
    for entry in protections["entries"]:
        if entry["table"] == "promo_offer_items":
            item = next((i for i in before["items"] if i["offer_item_id"] == entry["id"]), None)
            if item and item["offer_id"] in touched_offers:
                raise ValueError("protected child of changed Offer")
        if entry["table"] == "clinic_promotions" and entry["id"] == promotion_id:
            raise ValueError("protected source")
    plan = {"promotion_id": promotion_id, "business_id": source["business_id"], "url": source["source_url"],
            "before": graph_keys(before), "operations": operations, "expected": expected,
            "changed": changed, "expected_history": history, "new_ids": [c["id"] for c in new or []],
            "frozen_at": now(), "database_read_at": before["read_at"]}
    atomic_json(OUT / f"plan_{promotion_id}.json", plan)
    (OUT / f"apply_{promotion_id}.sql").write_text(transaction_sql(plan))
    (OUT / f"after_{promotion_id}.sql").write_text(snapshot_sql([source["business_id"]], auxiliary=True))
    return plan


def transaction_sql(plan):
    raw = json.dumps(plan, ensure_ascii=False, separators=(",", ":"))
    snapshot = snapshot_sql([plan["business_id"]], auxiliary=True).split("WITH ", 1)[1].rsplit(" ROLLBACK;", 1)[0].strip().removesuffix(";")
    snapshot = "WITH " + snapshot.replace(" AS snapshot", " INTO v_current")
    commands = []
    for index, op in enumerate(plan["operations"]):
        key, row_id = op["table"], op["id"]
        if key not in ALLOWED or not set(op["values"]) <= ALLOWED[key]:
            raise ValueError("unsafe field or table")
        table, pk = TABLES[key]
        columns = sorted(op["values"])
        record = f"jsonb_populate_record(NULL::public.{table}, v_plan->'operations'->{index}->'values')"
        if op.get("insert"):
            commands.append(f"INSERT INTO public.{table} ({','.join(columns)}) SELECT " + ",".join(f"x.{c}" for c in columns) + f" FROM {record} x;")
        else:
            assignments = ",".join(f"{c}=(x).{c}" for c in columns)
            commands.append(f"UPDATE public.{table} SET {assignments} FROM {record} x WHERE {table}.{pk}={row_id};\nIF NOT FOUND THEN RAISE EXCEPTION 'missing target {key}/{row_id}'; END IF;")
    locks = "\n".join(f"PERFORM 1 FROM public.{table} WHERE {pk} IN (SELECT (x->>'{pk}')::bigint FROM jsonb_array_elements(v_plan->'before'->'{key}') x) ORDER BY {pk} FOR UPDATE;" for key, (table, pk) in TABLES.items())
    expected_checks = []
    for key in ["businesses", "promotions", "offers", "items", "terms", "services", "confirmations", "audit", "queue"]:
        pk = TABLES.get(key, (None, "business_id" if key == "businesses" else "id"))[1]
        removals = " - 'updated_at'" + (" - 'offer_fingerprint'" if key == "offers" else "")
        current = f"CASE WHEN v_plan->'changed'->'{key}' @> jsonb_build_array((x->>'{pk}')::bigint) THEN x{removals} ELSE x END" if key in TABLES else "x"
        # New rows have server-generated creation/default values: require their
        # explicit expected fields; existing rows must match full frozen rows.
        expected_checks.append(f"""IF (SELECT count(*) FROM jsonb_array_elements(v_current->'{key}')) <> (SELECT count(*) FROM jsonb_array_elements(v_plan->'expected'->'{key}'))
 OR EXISTS (SELECT 1 FROM jsonb_array_elements(v_current->'{key}') x
 LEFT JOIN LATERAL (SELECT e FROM jsonb_array_elements(v_plan->'expected'->'{key}') e WHERE e->>'{pk}'=x->>'{pk}') z ON true
 WHERE z.e IS NULL OR CASE WHEN NOT EXISTS (SELECT 1 FROM jsonb_array_elements(v_plan->'before'->'{key}') old WHERE old->>'{pk}'=x->>'{pk}')
 THEN NOT (x @> z.e) ELSE ({current}) IS DISTINCT FROM z.e END)
 THEN RAISE EXCEPTION 'unexpected final state: {key}'; END IF;""")
    price_offer_ids = ids(plan["changed"]["offers"])
    fingerprint = "public.fn_build_offer_fingerprint(public.fn_offer_source_key(m.membership_plan_id,m.promotion_id,m.business_id), public.fn_offer_items_signature(m.id)||'|new_customer:'||coalesce(m.is_new_customer_required::text,'null')||'|conditions:'||coalesce(array_to_string(m.conditions,'||'),''),m.discount_price,m.discount_amount,m.discount_percent)"
    history_check = """IF EXISTS (SELECT 1 FROM jsonb_array_elements(v_plan->'before'->'history') h WHERE NOT (v_current->'history' @> jsonb_build_array(h))) THEN RAISE EXCEPTION 'history changed'; END IF;
SELECT coalesce(jsonb_agg(h - 'id' - 'changed_at' ORDER BY (h->>'offer_id')::bigint),'[]'::jsonb) INTO v_history FROM jsonb_array_elements(v_current->'history') h WHERE NOT EXISTS (SELECT 1 FROM jsonb_array_elements(v_plan->'before'->'history') old WHERE old->>'id'=h->>'id');
SELECT coalesce(jsonb_agg(h ORDER BY (h->>'offer_id')::bigint),'[]'::jsonb) INTO v_expected_history FROM jsonb_array_elements(v_plan->'expected_history') h;
IF v_history IS DISTINCT FROM v_expected_history THEN RAISE EXCEPTION 'unexpected price history'; END IF;"""
    command_sql = "\n".join(commands)
    check_sql = "\n".join(expected_checks)
    final_select = "WITH " + snapshot_sql([plan["business_id"]], auxiliary=True).split("WITH ", 1)[1].rsplit(" ROLLBACK;", 1)[0]
    delimiter = "$apply$"
    while delimiter in raw:
        delimiter = delimiter[:-1] + "_$"
    return f"""BEGIN TRANSACTION ISOLATION LEVEL SERIALIZABLE;
SET LOCAL lock_timeout='8s'; SET LOCAL statement_timeout='30s'; SET LOCAL standard_conforming_strings=on;
LOCK TABLE public.clinic_promotions,public.offer_confirmations,public.admin_audit_log IN SHARE ROW EXCLUSIVE MODE;
DO {delimiter}
DECLARE v_plan jsonb := {literal(raw)}::jsonb; v_current jsonb; v_history jsonb; v_expected_history jsonb;
BEGIN
{locks}
{snapshot};
IF (v_current - 'read_at' - 'project_id') IS DISTINCT FROM v_plan->'before' THEN RAISE EXCEPTION 'frozen snapshot/version/protection conflict'; END IF;
{command_sql}
SET CONSTRAINTS ALL IMMEDIATE;
{snapshot};
{check_sql}
{history_check}
IF EXISTS (SELECT 1 FROM public.promo_offer_master m WHERE m.id=ANY({price_offer_ids}) AND m.offer_fingerprint IS DISTINCT FROM {fingerprint}) THEN RAISE EXCEPTION 'stale fingerprint'; END IF;
IF EXISTS (SELECT 1 FROM public.promo_offer_master m JOIN public.promo_offer_items i ON i.offer_id=m.id WHERE m.id=ANY({price_offer_ids}) AND m.is_package AND i.unit_price IS NOT NULL) THEN RAISE EXCEPTION 'package unit price remaining'; END IF;
IF EXISTS (SELECT 1 FROM public.promo_offer_master m JOIN public.clinic_promotions p ON p.promotion_id=m.promotion_id WHERE m.id=ANY({price_offer_ids}) AND m.is_active AND (NOT p.is_active OR p.campaign_end_date<current_date)) THEN RAISE EXCEPTION 'active offer under inactive or expired parent'; END IF;
IF EXISTS (SELECT 1 FROM public.promo_offer_master m JOIN public.promo_offer_items i ON i.offer_id=m.id LEFT JOIN public.clinic_services s ON s.service_id=i.service_id WHERE m.id=ANY({price_offer_ids}) AND m.is_active AND i.service_id IS NOT NULL AND (s.business_id IS DISTINCT FROM m.business_id OR i.item_name IS DISTINCT FROM s.service_name::text)) THEN RAISE EXCEPTION 'item service ownership/name mismatch'; END IF;
IF EXISTS (SELECT 1 FROM public.promo_offer_master m JOIN public.promo_offer_items i ON i.offer_id=m.id WHERE m.id=ANY({price_offer_ids}) AND m.is_active AND NOT m.is_package AND m.discount_price IS NOT NULL AND (i.unit_price IS NULL OR round(i.unit_price*coalesce(i.delivered_units,1),2)<>round(m.discount_price,2))) THEN RAISE EXCEPTION 'unit/quantity mismatch'; END IF;
IF EXISTS (SELECT 1 FROM public.promo_offer_master m JOIN public.promo_offer_items i ON i.offer_id=m.id WHERE m.is_active AND i.service_id IS NOT NULL AND m.offer_raw_text ~* '(all services|any service|all products|all treatments|gift card|skin ?care products)') THEN RAISE EXCEPTION 'blanket service association'; END IF;
END {delimiter};
{final_select}
COMMIT;
"""


def op(table, row_id, **values):
    return {"table": table, "id": row_id, "values": values}


def freeze():
    all_plans = []
    all_plans.append(make_plan(3595, [op("promotions", 3595, is_active=False)]))
    laser = load(OUT / "aux_168.json")
    p = next(p for p in laser["promotions"] if p["promotion_id"] == 168)
    changes = [op("promotions", 168, campaign_end_date="2026-10-31", is_offer_extracted=False,
                  promotion_content=["Xeomin $9.99/unit.", "Botox $9.99/unit.", "Dysport $12/unit.",
                                     "Versa Filler $650/syringe; a full 1.2mL syringe of Revanesse Versa.",
                                     "Juvéderm Filler $750/syringe.", "Morpheus8, Face & Neck $2,499 for 3 treatments.",
                                     "Specials valid through October 31, 2026. Cannot be combined with other offers."])]
    for oid in [8686, 8687, 8690]:
        m = next(m for m in laser["offers"] if m["id"] == oid)
        text = m["offer_raw_text"].replace("September 30, 2026", "October 31, 2026")
        values = {"offer_raw_text": text, "conditions": [c.replace("September 30, 2026", "October 31, 2026") for c in m["conditions"]]}
        if oid == 8687:
            values.update(discount_price=12, offer_raw_text=text.replace("$10.99", "$12"))
        changes.append(op("offers", oid, **values))
    changes.append(op("offers", 8688, is_active=False))
    all_plans.append(make_plan(168, changes))
    allure = load(OUT / "aux_217.json")
    content = next(p for p in allure["promotions"] if p["promotion_id"] == 217)["promotion_content"]
    content = ["Jeuveau $9/unit; regular $10/unit. Minimum 50 units; all units used during the same appointment." if s.startswith("Letybo:") else
               "VI Peel $300; regular $350/peel. Add a VI Peel to any Toxin appointment for $250; treatments must be completed during the same appointment by the same provider." if s.startswith("VI Peel:") else
               s + " Packages include 6 treatments." if s.startswith("Laser Hair Removal packages:") else s for s in content]
    content += ["Radiofrequency Microneedling $650; regular $750/treatment.",
                "Radiesse $75 off; regular $699/syringe or $849/hyperdiluted treatment. Register with Xperience+ to redeem the discount."]
    changes = [op("promotions", 217, is_offer_extracted=False, promotion_content=content)]
    for oid, regular in [(8129, 1250), (8130, 1500), (8136, 2000)]:
        changes.append(op("offers", oid, regular_price=regular))
    changes += [op("offers", 8127, is_package=True), op("items", 2164, delivered_units=6, unit_price=None),
                op("offers", 8126, conditions=["add a VI Peel to any Toxin appointment", "treatments completed during same appointment by same provider"], offer_raw_text="VI Chemical Peels: add a VI Peel to any Toxin appointment for ONLY $250 (reg. $350/peel); treatments completed during the same appointment by the same provider."),
                op("offers", 8025, is_active=False)]
    for item in allure["items"]:
        if item["offer_id"] in {8129, 8130, 8136}:
            changes.append(op("items", item["offer_item_id"], unit_price=None))
    reserved = load(OUT / "reserved_ids.json") if (OUT / "reserved_ids.json").exists() else None
    new = []
    if reserved:
        master_id, item_id = reserved["offer_id"], reserved["item_id"]
        new = [{**op("offers", master_id, id=master_id, business_id=3079, promotion_id=217, membership_plan_id=None,
                      offer_raw_text="VI Chemical Peels: $300 (reg. $350/peel).", regular_price=350, discount_price=300,
                      discount_amount=50, discount_percent=14.29, price_model="total", is_package=False, conditions=[],
                      is_active=False, is_new_customer_required=False, is_membership_required=False), "insert": True},
               {**op("items", item_id, offer_item_id=item_id, offer_id=master_id, item_name="Chemical Peel", service_id=3335, delivered_units=1, unit_price=300), "insert": True}]
    all_plans.append(make_plan(217, changes, new=new))
    all_plans.append(make_plan(176, [op("promotions", 176, is_offer_extracted=False)]))
    changes = []
    scope = {9805: "Small area: lip or chin", 9806: "Medium area: underarms, bikini, or Brazilian", 9807: "Large area: back, arms, or legs"}
    aqua = load(OUT / "aux_218.json")
    for oid in [9805, 9806, 9807, 9808, 9809]:
        values = {"is_new_customer_required": True, "conditions": ["Specials valid for new patients only", "Limit one special per patient"] + ([scope[oid]] if oid in scope else [])}
        if oid in scope:
            values["is_package"] = True
            for item in aqua["items"]:
                if item["offer_id"] == oid:
                    changes.append(op("items", item["offer_item_id"], unit_price=None))
        changes += [op("offers", oid, **values), op("terms", oid, is_one_per_patient=True)]
    all_plans.append(make_plan(218, changes))
    all_plans.append(make_plan(216, [op("offers", 8519, is_package=True), op("items", 1600, unit_price=None), op("offers", 10424, is_package=True), op("items", 5094, unit_price=None)]))
    all_plans.append(make_plan(697, [op("promotions", 697, is_active=False)]))
    atomic_json(OUT / "frozen_plan.json", {"frozen_at": now(), "schema_changes": False, "scope_pages": 189, "plans": all_plans, "new_offer_pending_reservation": not bool(reserved)})
    print(json.dumps({"sources": len(all_plans), "operations": sum(len(p["operations"]) for p in all_plans), "new_ids_reserved": bool(reserved)}))


def reconcile(plan, snapshot):
    current = graph_keys(snapshot)
    matched = True
    for key in current:
        if key == "history":
            continue
        pk = TABLES.get(key, (None, "business_id" if key == "businesses" else "id"))[1]
        old_ids = {r[pk] for r in plan["before"][key]}
        actual = {r[pk]: r for r in current[key]}
        desired = {r[pk]: r for r in plan["expected"][key]}
        if actual.keys() != desired.keys():
            matched = False
            continue
        for row_id, want in desired.items():
            got = deepcopy(actual[row_id])
            if row_id in plan["changed"].get(key, []):
                got.pop("updated_at", None)
                if key == "offers":
                    got.pop("offer_fingerprint", None)
            if row_id not in old_ids:
                if any(got.get(k) != v for k, v in want.items()):
                    matched = False
            elif got != want:
                matched = False
    history_ids = {h["id"] for h in plan["before"]["history"]}
    current_history = {h["id"]: h for h in current["history"]}
    if any(current_history.get(h["id"]) != h for h in plan["before"]["history"]):
        matched = False
    added = [{k: v for k, v in h.items() if k not in {"id", "changed_at"}} for h in current["history"] if h["id"] not in history_ids]
    if sorted(added, key=lambda h: h["offer_id"]) != sorted(plan["expected_history"], key=lambda h: h["offer_id"]):
        matched = False
    if matched:
        return "confirmed_current_state"
    # This is a retry candidate, not proof the first request has rolled back.
    # The generated transaction reacquires locks and rechecks this same graph.
    if current == plan["before"]:
        return "retry_same_plan_under_lock"
    return "review_conflict_or_partial_state"


def write_csv(path, rows, columns):
    with Path(path).open("w", encoding="utf-8-sig", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=columns, extrasaction="ignore")
        writer.writeheader()
        writer.writerows(rows)


def read_csv(path):
    with Path(path).open(encoding="utf-8-sig", newline="") as handle:
        return list(csv.DictReader(handle))


def report():
    frozen = load(OUT / "frozen_plan.json")
    before = load(OUT / "database_before.json")
    after = load(OUT / "database_after.json")
    source_refs = {r["promotion_id"]: r["files"] for r in load(OUT / "source_evidence.json")}
    quotes = {
        3595: "September new-patient specials: available for purchase 09/01/26–09/30/26; treatments must be completed by 12/31/26.",
        168: "October Specials — Available through October 31, 2026. Botox $9.99/unit; Xeomin $9.99/unit; Dysport $12/unit; Versa Filler $650/syringe; Juvéderm Filler $750/syringe; Morpheus8, Face & Neck $2,499 for 3 treatments. Cannot be combined with other offers.",
        217: "VI Chemical Peels: $300 OR Add a VI Peel to any Toxin Appointment for ONLY $250! (reg. price: $350/peel); treatments must be completed during same appointment by same provider. Packages include 6 treatments: Upper Lip or Chin $750/$525; Face $1250/$875; Face + Beard $1500/$1050; Legs (Knees and Below) $2000/$1400. Jeuveau $9/unit, minimum 50 units.",
        176: "3 SkinPen + 3 VAMP + 1 FREE SkinBetter Peel ($325 in savings); Purchase 8 Venus Legacy treatments for one area, receive 3 FREE Emsculpt arm treatments ($750 in savings).",
        218: "$215 3 treatments of a small area; $320 3 treatments of a medium area; $540 3 treatments of a large area. Small: lip, chin. Medium: underarms, bikini, Brazilian. Large: back, arms, legs. Specials valid for new patients only. Limit one special per patient.",
        216: "3 Morpheus RF Microneedling Sessions $2499 (reg. $3000); 3 IPL Sessions $975 (reg. $1200).",
        697: "No Deals Right Now, But… We’re between promotions at the moment. Existing campaign and all three active Offers explicitly expire September 30, 2026.",
    }
    expected_full = {k: {r[TABLES.get(k, (None, "business_id" if k == "businesses" else "id"))[1]]: r for r in before[k]} for k in ["businesses", "promotions", "offers", "items", "terms", "services", "confirmations", "audit"]}
    changes = []
    fields = ["change_id", "business_id", "business_name", "url", "promotion_id", "offer_id", "item_id", "table", "field", "old_value", "new_value", "action", "status", "reason", "evidence_quote", "evidence_paths", "database_before_at", "database_after_at", "receipt"]
    business_names = {b["business_id"]: b["name"] for b in after["businesses"]}
    stats = Counter()
    for plan in frozen["plans"]:
        pid = plan["promotion_id"]
        actual = load(OUT / f"after_{pid}.json")
        assert reconcile(plan, actual) == "confirmed_current_state"
        for key in expected_full:
            pk = TABLES.get(key, (None, "business_id" if key == "businesses" else "id"))[1]
            expected_full[key].update({r[pk]: r for r in actual[key]})
        for key in ["promotions", "offers", "items", "terms"]:
            table, pk = TABLES[key]
            old_rows = {r[pk]: r for r in plan["before"][key]}
            current = {r[pk]: r for r in actual[key]}
            for row_id in plan["changed"][key]:
                old, new = old_rows.get(row_id), current[row_id]
                differences = {"record": (None, {k: v for k, v in new.items() if k not in {"updated_at", "created_at", "offer_fingerprint"}})} if old is None else {k: (old.get(k), v) for k, v in new.items() if k not in {"updated_at", "offer_fingerprint"} and old.get(k) != v}
                if not differences:
                    stats["version_only_rows"] += 1
                elif key == "offers":
                    stats["new_offers" if old is None else "deactivated_offers" if old["is_active"] and not new["is_active"] else "updated_offers"] += 1
                elif key == "promotions":
                    stats["deactivated_promotions" if old["is_active"] and not new["is_active"] else "updated_promotions"] += 1
                elif key == "items":
                    stats["new_items" if old is None else "updated_items"] += 1
                else:
                    stats["updated_terms"] += 1
                for field, (old_value, new_value) in differences.items():
                    oid = row_id if key == "offers" else new.get("offer_id", "")
                    changes.append({"change_id": f"AP{len(changes)+1:05d}", "business_id": plan["business_id"], "business_name": business_names[plan["business_id"]], "url": plan["url"], "promotion_id": pid, "offer_id": oid, "item_id": row_id if key == "items" else "", "table": table, "field": field, "old_value": json.dumps(old_value, ensure_ascii=False), "new_value": json.dumps(new_value, ensure_ascii=False), "action": "新增" if old is None else "停用" if field == "is_active" and new_value is False else "更新", "status": "已提交并回读验证", "reason": "按来源冻结计划执行；触发器及完整关联状态断言通过", "evidence_quote": quotes[pid], "evidence_paths": json.dumps(source_refs[pid], ensure_ascii=False), "database_before_at": plan["database_read_at"], "database_after_at": actual["read_at"], "receipt": str(OUT / f"receipt_{pid}.json")})
        stats["price_history_added"] += len(actual["history"]) - len(plan["before"]["history"])
    unexpected = []
    for key, expected in expected_full.items():
        pk = TABLES.get(key, (None, "business_id" if key == "businesses" else "id"))[1]
        current = {r[pk]: r for r in after[key]}
        for row_id in expected.keys() | current.keys():
            if expected.get(row_id) != current.get(row_id):
                unexpected.append({"table": key, "id": row_id})
    assert not unexpected, unexpected
    checks = load(OUT / "final_database_checks.json")
    schema_before = load(OUT / "schema_before.json")["main"]
    sort_rows = lambda rows: sorted(rows, key=lambda row: json.dumps(row, sort_keys=True))
    assert sort_rows(schema_before["columns"]) == sort_rows(checks["columns"] + checks["service_columns"])
    assert sort_rows(schema_before["triggers"]) == sort_rows(checks["triggers"])
    assert sort_rows(schema_before["constraints"]) == sort_rows(checks["constraints"])
    assert checks["blanket"] == []
    protected = load(OUT / "manual_protections.json")
    for entry in protected["entries"]:
        for key, (table, pk) in TABLES.items():
            if table != entry["table"] or key not in expected_full:
                continue
            old = next((r for r in before[key] if r[pk] == entry["id"]), None)
            current = next((r for r in after[key] if r[pk] == entry["id"]), None)
            assert old == current, entry
    write_csv(OUT / "apply_changes.csv", changes, fields)
    def change_pk(row):
        return row["item_id"] if row["table"] == "promo_offer_items" else row["promotion_id"] if row["table"] == "clinic_promotions" else row["offer_id"]
    actual_fields = {(r["table"], str(change_pk(r)), r["field"]): r for r in changes}
    input_rows = read_csv(OUT.parent / "reread_20261011/dryrun_changes_with_reread.csv")
    assert len(input_rows) == 1072 and len({r["row_id"] for r in input_rows}) == 1072
    input_rows += read_csv(OUT.parent / "reread_20261011/dryrun_changes.csv")
    for index, row in enumerate(read_csv(OUT.parent / "inactive_check_20261011/inactive_check.csv"), 1):
        row = {**row, "row_id": f"IC{index:05d}", "proposed_value": row["suggested_value"]}
        if row["table"] == "clinic_promotions":
            row["promotion_id"] = row["record_id"]
        elif row["table"] == "promo_offer_master":
            row.update(offer_id=row["record_id"], promotion_id="3595")
        input_rows.append(row)
    result_rows = []
    no_op_tokens = ["拒绝", "历史覆盖不建议", "保留原条件", "2018历史来源", "无需同步", "无可停用记录", "已经停用", "已 inactive", "未发现移除", "不支持停用"]
    for row in input_rows:
        table = row.get("table", "source")
        row_id = row.get("offer_id") if table == "promo_offer_master" else row.get("item_id") if table == "promo_offer_items" else row.get("promotion_id")
        applied = actual_fields.get((table, str(row_id or ""), row.get("field")))
        action = row.get("host_action") or row.get("action", "")
        reason = row.get("host_reason") or row.get("reason") or row.get("decision", "")
        if applied:
            state, decision = "已更新", "实际最终值见 " + applied["change_id"] + "；采用冻结计划而非盲用原建议值"
        elif row.get("row_id") in {"DR00912", "DR00913", "DR00939"}:
            state, decision = "已更新", "VI Peel300独立档位已新增；8126保留active并补齐250搭配条件，撤销旧停用疑点"
        elif row.get("row_id") in {"DR00023", "DR00025", "DR00027", "DR00029", "DR00030", "DR00724"}:
            state, decision = "无需操作", "截图已核对：现有原文的价格、交付次数及服务语义一致；资格/套餐变化另有实际更新，不做纯格式替换"
        elif str(row.get("promotion_id", "")) in {"697", "3595"}:
            state, decision = "无需操作", "来源及所有active下游已按计划停用；保留历史原文与Items，不改写历史报价"
        elif row.get("row_id") == "RR00043":
            state, decision = "已更新", "已新增独立 VI Peel $300，Offer10721 / Item5511；保留$250有条件档位"
        elif any(token in action + reason + row.get("inactive_check_decision", "") for token in no_op_tokens) or action in {"未发现差异", "无可更新记录", "未发现移除"}:
            state, decision = "无需操作", action + "；仅限已有证据比较的字段/已排除建议"
        else:
            state, decision = "待审查", "; ".join(filter(None, [action, reason, row.get("host_execution_blockers"), row.get("execution_blockers")])) or "证据不足以构成可安全执行的变更"
        result_rows.append({**row, "execution_status": state, "execution_decision": decision, "actual_value": applied["new_value"] if applied else "", "apply_change_id": applied["change_id"] if applied else "", "final_database_read_at": after["read_at"]})
    result_columns = list(dict.fromkeys(k for row in result_rows for k in row))
    write_csv(OUT / "finding_results.csv", result_rows, result_columns)
    reviews = [r for r in result_rows if r["execution_status"] == "待审查"]
    write_csv(OUT / "review_list.csv", reviews, result_columns)
    visual = load(OUT.parent / "visual_review_20261010/visual_review_evidence.json")
    pages = []
    ready_sources = {normalize_source_url(p["url"]): p for p in frozen["plans"]}
    source_promos = {}
    for p in after["promotions"]:
        if p.get("source_url"):
            source_promos.setdefault(normalize_source_url(p["source_url"]), []).append(p["promotion_id"])
    no_offer_jobs = {"VR092", "VR128", "VR131", "VR164", "VR016", "VR105", "VR132"}
    for page in visual["page_coverage"]:
        canonical = page["canonical_url"]
        relevant = [r for r in result_rows if r.get("url") and normalize_source_url(r["url"]) == canonical]
        review_count = sum(r["execution_status"] == "待审查" for r in relevant)
        if canonical in ready_sources:
            plan = ready_sources[canonical]
            status = "已更新"
            note = "来源级事务提交并回读通过；停用" if plan["promotion_id"] in {3595, 697} else "部分同步；未处理候选保留审查，未宣称完整同步"
            if plan["promotion_id"] in {3595, 697}:
                review_count = 0
        elif page.get("visual_job_id") in no_offer_jobs:
            status, note = "无需操作", "用户已确认无优惠或明确404；当前无对应active记录，不停用其他URL活动"
            review_count = 0
        elif review_count:
            status, note = "待审查", "商家/活动归属、服务、日期、历史状态、人工保护或计价口径尚未全部解决"
        else:
            status, note = "无需操作", "已有证据未形成可直接执行的变更；一致结论仅限已比较字段"
        pages.append({"url": page["url"], "canonical_url": canonical, "business_id": page["business_id"], "promotion_ids": json.dumps(source_promos.get(canonical, [])), "status": status, "note": note, "remaining_review_findings": review_count, "primary_captured_at": page["primary_captured_at"], "visual_observed_at": page.get("visual_observed_at", ""), "visual_review_path": page.get("visual_review_path", ""), "primary_evidence_path": page["primary_path"], "final_database_read_at": after["read_at"]})
    assert len(pages) == 189 and len({p["canonical_url"] for p in pages}) == 179
    write_csv(OUT / "page_results.csv", pages, list(pages[0]))
    page_counts = dict(Counter(p["status"] for p in pages))
    source_counts = dict(Counter({p["canonical_url"]: p["status"] for p in pages}.values()))
    verdict = {"verdict": "PASS", "generated_at": now(), "pages": 189, "canonical_sources": 179,
               "page_results": page_counts, "source_results": source_counts, "applied_sources": 7,
               "applied_configured_sources": source_counts.get("已更新", 0), "applied_supplemental_sources": 1,
               "actual_field_changes": len(changes), "remaining_review_findings": len(reviews),
               "counts": dict(stats), "full_graph_unexpected_differences": unexpected,
               "blanket_rows": 0, "schema_columns_triggers_constraints_unchanged": True,
               "protected_records_unchanged": True, "historical_protected_id_missing_before_and_after": [7594],
               "database_transport": "Supabase MCP", "database_password_required": False,
               "ddl_calls": 0, "monitor_or_scheduler_mutations": 0,
               "replay_rejected_before_writes": True, "local_tests_passed": 9,
               "scope_note": "PASS means all executable frozen changes verified and every page classified; remaining review sources are not claimed synchronized."}
    atomic_json(OUT / "verification.json", verdict)
    atomic_json(OUT / "frozen_plan.json", {**frozen, "coverage_file": "page_results.csv", "finding_results_file": "finding_results.csv", "review_file": "review_list.csv"})
    log = load(OUT / "execution_log.json")
    atomic_json(OUT / "execution_log.json", {**log, "status": "complete", "completed_at": now()})
    report_text = f"""# 本次核验数据库更新结果

结论：**PASS**。项目 `kdlpkjzcnbkjcvwsvlwn`。仅应用可确认部分；待审查来源未宣称完成同步。

189页全部有结果：已更新{page_counts.get('已更新',0)}页，无需操作{page_counts.get('无需操作',0)}页，待审查{page_counts.get('待审查',0)}页。
按179个唯一来源统计：已更新{source_counts.get('已更新',0)}个，无需操作{source_counts.get('无需操作',0)}个，待审查{source_counts.get('待审查',0)}个。
7个提交来源中，6个属于配置的189页范围；JTN3595是已授权方案明确列出的同商家月度活动补充来源，其URL不在原189页配置内。
共提交7个来源事务，更新15条已有优惠，新增1条优惠，停用6条优惠及2项活动；保留所有已有Offer、Item ID。新增Offer **10721**、Item **5511**。
实际字段差异{len(changes)}项，待审查发现{len(reviews)}条。审查条目不是确定错误或可直接执行的更新。

| 来源 | 实际处理 | 剩余边界 |
| --- | --- | --- |
| JTN 3595 | 活动和Offer10397停用 | 其他URL、已inactive历史记录未改变 |
| LaserLight 168 | Dysport12/unit；现有Botox/Xeomin及Dysport有效期10/31/2026；停用页面已移除Lip Flip8688；更新有证据正文 | 历史3589保持inactive；Versa/Juvéderm/Morpheus映射和历史状态仍审查 |
| Allure 217 | 三组原价及折扣；Upper Lip六次套餐；VI Peel250资格；新增独立300档位；停用Letybo8025；正文和完成标志 | Jeuveau/RF/其他套餐及Radiesse映射，CO2金额标签冲突仍审查 |
| Belle176 | is_offer_extracted=false | 套餐组成、缺失服务及活动年份仍审查 |
| Acqua Blu218 | 五条新客+每人限一次；三组3次套餐标志和NULL单价 | 保留分类服务及备选部位；完成标志保持false |
| Relive216 | 两组三次套餐标志及NULL单价 | 保留既有总价/数量；完成标志保持false |
| Radiance697 | 已过期活动及三条优惠停用 | 新客页P232及所有历史Items保持原状 |

## 验证证据

- 每来源在串行化事务内核验完整旧值/版本、条款、队列和价格历史，再强制延迟约束与最终断言；7份回执均为confirmed_current_state。
- 末次全范围回读：93个商家、126活动、968 Offers、1057 Items、968条款、606服务关联信息；与更新前快照加实际提交结果完全一致，计划外差异为0。
- Blanket检查0行；列定义、约束及触发器定义未改变；已有人工保护记录及版本未改变。历史保护ID7594在更新前后都不存在，未恢复。
- 新增价格历史4条，均由现有触发器写入；没有新增内容队列。
- 本地9项针对性测试通过，覆盖套餐清价、最低门槛、条件/条款、保留ID、派生折扣、保护/版本冲突、SQL输入和未知提交回读。
- 实际重放Allure同一计划被冻结版本检查拒绝；无重复创建。首个JTN尝试因服务名称枚举比较缺少类型转换而整笔回滚，回读旧状态后修正并重试；错误记录已保留。
- 没有执行DDL、迁移、RPC创建、服务价写入、监控任务入口或基线/定时任务修改。

## 交付文件

- `apply_changes.csv`：实际字段变更及新记录。
- `page_results.csv`：全部189页最终归类。
- `finding_results.csv`：原1072条发现、58条补充及inactive检查的逐条结果。
- `review_list.csv`：未写入的剩余审查条目。
- `frozen_plan.json`、`database_before.json`、`database_after.json`、`manual_protections.json`、逐来源计划与回执：恢复和追溯依据。
- `recovery.md`：提交结果不明与重试说明。

NewLook日期归属和人工保护8590、Advanced8530历史状态/年份、Fix活动换期等保留原值。三个无优惠页面与其他404没有可停用active记录，未进行跨URL停用。全范围读取分批保存，单个来源写入为原子事务；不把全范围快照声称为同一时间点。
"""
    (OUT / "apply_report.md").write_text(report_text)
    (OUT / "recovery.md").write_text("""# 恢复说明

本批次已完成，禁止直接再次应用旧SQL；它包含旧版本断言，正常重放会被拒绝。

提交结果不明时先保存异常和原计划，不重新分配ID；通过对应after_<promotion_id>.sql回读完整关联集合。
运行reconcile：confirmed_current_state只确认当前业务状态与计划一致，保存当前实际版本作为回执；不证明此后无人进行语义相同修改。
retry_same_plan_under_lock不是原事务已回滚的证明。最多重试一次相同SQL和相同ID；事务先取得锁，再比较完整旧状态、版本、保护/条款/历史/队列集合，任何不同都抛错，不继续逐行写入。
review_conflict_or_partial_state必须保留当前数据库和日志，人工复核；不得盲目重建、反向覆盖、删除新记录或恢复历史活动。

若需纠正已提交业务值，应根据新证据重新冻结变更计划与当前版本；本次回执和历史记录保留。数据库Schema无需任何恢复。
""")
    print(json.dumps(verdict, ensure_ascii=False))


if __name__ == "__main__":
    {"bootstrap": bootstrap, "combine": combine, "freeze": freeze, "report": report}[sys.argv[1]]()
