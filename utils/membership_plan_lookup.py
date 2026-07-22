"""Read helpers: resolve membership plan fields via FK join."""
from __future__ import annotations

import json
import re
from typing import Any, Dict, Optional


def _clean(value: Any) -> str:
    return re.sub(r"\s+", " ", str(value or "").strip())


def plan_display_name(plan_row: Optional[Dict[str, Any]]) -> str:
    if not plan_row:
        return ""
    return _clean(
        plan_row.get("membership_name")
        or plan_row.get("tier_name")
        or plan_row.get("plan_name")
        or plan_row.get("plan_tier_name")
        or plan_row.get("plan_display_name")
    )


def normalize_plan_name(value: Any) -> str:
    return re.sub(r"[^a-z0-9]+", " ", _clean(value).lower()).strip()


def resolve_plan_fields(
    offer_row: Dict[str, Any],
    plan_row: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    """Merge offer + plan display fields (from enriched view columns or plan row)."""
    merged = dict(offer_row)
    embedded = merged.pop("clinic_memberships", None) or merged.pop("promo_membership_plans", None)
    if plan_row is None and isinstance(embedded, dict):
        plan_row = embedded
    display = _clean(
        (plan_row or {}).get("membership_name")
        or (plan_row or {}).get("plan_name")
        or merged.get("plan_display_name")
        or (plan_row or {}).get("plan_display_name")
        or (plan_row or {}).get("tier_name")
    )
    billing = _clean(
        (plan_row or {}).get("billing_period")
        or merged.get("plan_billing_period")
    )
    fee = (plan_row or {}).get("membership_price")
    if fee is None:
        fee = merged.get("plan_monthly_fee")
    if fee is None and plan_row:
        fee = plan_row.get("monthly_fee") or plan_row.get("annual_fee")

    merged["membership_display_name"] = display
    merged["membership_billing_period"] = billing
    merged["membership_plan_fee"] = fee
    perks = (plan_row or {}).get("perks") or (plan_row or {}).get("benefits")
    if isinstance(perks, str):
        try:
            merged["membership_perks"] = json.loads(perks)
        except json.JSONDecodeError:
            merged["membership_perks"] = perks
    elif perks is not None:
        merged["membership_perks"] = perks
    return merged
