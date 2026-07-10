"""Read helpers: resolve membership plan fields via FK join."""
from __future__ import annotations

import re
from typing import Any, Dict, Optional


def _clean(value: Any) -> str:
    return re.sub(r"\s+", " ", str(value or "").strip())


def plan_display_name(plan_row: Optional[Dict[str, Any]]) -> str:
    if not plan_row:
        return ""
    tier = _clean(plan_row.get("tier_name") or plan_row.get("plan_tier_name"))
    name = _clean(plan_row.get("plan_name") or plan_row.get("plan_display_name"))
    return tier or name


def normalize_plan_name(value: Any) -> str:
    return re.sub(r"[^a-z0-9]+", " ", _clean(value).lower()).strip()


def resolve_plan_fields(
    offer_row: Dict[str, Any],
    plan_row: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    """Merge offer + plan display fields (from enriched view columns or plan row)."""
    merged = dict(offer_row)
    embedded = merged.pop("promo_membership_plans", None)
    if plan_row is None and isinstance(embedded, dict):
        plan_row = embedded
    tier = _clean(
        (plan_row or {}).get("tier_name")
        or merged.get("plan_tier_name")
        or (plan_row or {}).get("plan_tier_name")
    )
    display = _clean(
        (plan_row or {}).get("plan_name")
        or merged.get("plan_display_name")
        or (plan_row or {}).get("plan_display_name")
    )
    billing = _clean(
        (plan_row or {}).get("billing_period")
        or merged.get("plan_billing_period")
    )
    monthly = merged.get("plan_monthly_fee")
    if plan_row and plan_row.get("monthly_fee") is not None:
        monthly = plan_row.get("monthly_fee")
    annual = merged.get("plan_annual_fee")
    if plan_row and plan_row.get("annual_fee") is not None:
        annual = plan_row.get("annual_fee")

    merged["membership_display_name"] = tier or display
    merged["membership_billing_period"] = billing
    merged["membership_plan_fee"] = monthly if monthly is not None else annual
    return merged
