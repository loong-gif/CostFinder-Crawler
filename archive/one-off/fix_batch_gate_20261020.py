"""Clear batch gate G/H/I failures found on 2026-10-20 (dry-run unless --apply).

G: BBL + MOXI "3 SESSIONS" packages store delivered_units=3.
H: member tiers carry the non-member price as regular_price; non-member siblings deactivated.
I: inactive itemless shells are deleted after a full backup (refuses if anything user-facing references them).
"""
import json
import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from dotenv import load_dotenv

load_dotenv(ROOT / ".env")
from utils.batch_gate_checks import CHECKS, linked_query
from utils.supabase_access import fetch_all_rows, load_supabase_client

G_UNITS = {5499: 3, 5500: 3, 5507: 3, 5508: 3}
H_REGULAR = {4373: 10, 7662: 11, 10016: 5.5, 9995: 6.5, 8649: 1758, 8650: 1758, 8652: 889,
             8654: 3955, 10769: 3955, 8656: 1125, 10771: 975}
NON_MEMBER_SIBLINGS = [9998, 9999, 10015, 10017, 8648, 8651, 8653, 8655, 10768, 10770]
USER_REFS = ("claims", "saved_deals")
NULLED_REFS = ("listing_requests", "deal_reports", "offer_confirmations")
CHILDREN = ("promo_offer_terms", "promo_offer_price_history", "promo_offer_items")
OUT = ROOT / "output/gate_fix_20261020"


def ids(values):
    return f"in.({','.join(map(str, values))})"


def main(apply: bool) -> None:
    c = load_supabase_client(role="service")
    i_rows = linked_query(CHECKS["I_itemless_offer"], root=ROOT)
    active = [r["id"] for r in i_rows if r["is_active"]]
    if active:
        raise SystemExit(f"active itemless offers need a re-sync, not deletion: {active}")
    shells = sorted(r["id"] for r in i_rows)
    backup = {"offers": fetch_all_rows(c, "promo_offer_master", "*", filters={"id": ids(shells)}) if shells else []}
    for t in USER_REFS:
        backup[t] = fetch_all_rows(c, t, "*", filters={"deal_id": ids(shells)}) if shells else []
    for t in (*NULLED_REFS, *CHILDREN):
        backup[t] = fetch_all_rows(c, t, "*", filters={"offer_id": ids(shells)}) if shells else []
    blocked = {t: len(backup[t]) for t in (*USER_REFS, *NULLED_REFS) if backup[t]}
    if blocked:
        raise SystemExit(f"itemless shells are referenced, review first: {blocked}")
    backup["h_offers"] = fetch_all_rows(c, "promo_offer_master", "*",
                                        filters={"id": ids([*H_REGULAR, *NON_MEMBER_SIBLINGS])})
    backup["g_items"] = fetch_all_rows(c, "promo_offer_items", "*", filters={"offer_item_id": ids(G_UNITS)})
    print({"delete_shells": len(shells), "children": {t: len(backup[t]) for t in CHILDREN},
           "h_updates": len(H_REGULAR), "deactivate": len(NON_MEMBER_SIBLINGS), "g_items": len(G_UNITS)})
    if not apply:
        return
    if os.environ.get("ALLOW_SERVICE_ROLE_WRITES") != "1":
        raise SystemExit("set ALLOW_SERVICE_ROLE_WRITES=1")
    OUT.mkdir(parents=True, exist_ok=True)
    if (OUT / "backup_before_fix.json").exists():
        raise SystemExit("backup_before_fix.json exists; this script already ran, never overwrite its backup")
    (OUT / "backup_before_fix.json").write_text(json.dumps(backup, ensure_ascii=False, indent=1, default=str))

    changed = []
    for item_id, units in G_UNITS.items():
        changed += c.update_row("promo_offer_items", {"offer_item_id": f"eq.{item_id}", "delivered_units": "is.null"},
                                {"delivered_units": units})
    for offer_id, regular in H_REGULAR.items():
        changed += c.update_row("promo_offer_master", {"id": f"eq.{offer_id}", "regular_price": "is.null",
                                                       "is_membership_required": "is.true"}, {"regular_price": regular})
    changed += c.update_row("promo_offer_master", {"id": ids(NON_MEMBER_SIBLINGS), "is_active": "is.true",
                                                   "is_membership_required": "is.false"}, {"is_active": False})
    deleted = c.delete_rows("promo_offer_master", {"id": ids(shells), "is_active": "is.false"}) if shells else []
    report = {"updated_rows": len(changed), "deleted_shells": len(deleted)}
    (OUT / "apply_report.json").write_text(json.dumps(report, indent=1))
    print(report)


if __name__ == "__main__":
    main("--apply" in sys.argv)
