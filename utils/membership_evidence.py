"""Membership claims share the rendered, capture-bound evidence boundary."""
from __future__ import annotations
import math
import re
from typing import Any, Mapping
from utils.rendered_visibility_gate import filter_visible_membership_benefits


def prepare_membership_write(item: dict[str, Any], evidence: str, rendered_visibility: Mapping[str, Any] | None = None) -> dict[str, Any]:
    name = str(item.get('membership_name') or item.get('plan_name') or '').strip()
    price = item.get('membership_price')
    quote = str(item.get('membership_description') or item.get('evidence_quote') or '').strip()
    # Bind the fee to the named plan's own quotation; a page-wide fee match can
    # accidentally borrow another tier's fee.
    source = quote if quote and quote.casefold() in evidence.casefold() else ''
    if not source:
        source = '\n'.join(line for line in evidence.splitlines() if name and name.casefold() in line.casefold())
    try:
        fee = float(price)
    except (TypeError, ValueError):
        return {'accepted': False, 'reason': 'missing_membership_or_price'}
    amount = re.escape(f'{fee:g}')
    if not name or isinstance(price, bool) or not math.isfinite(fee) or fee <= 0 or name.casefold() not in source.casefold() or not re.search(r'[$€£]\s*' + amount + r'(?:\.0{1,2})?(?![\d.])', source.replace(',', '')):
        return {'accepted': False, 'reason': 'membership_fee_not_supported_by_named_evidence'}
    benefits = item.get('benefits')
    verified = None
    if isinstance(benefits, list) and benefits:
        values = [str(value.get('desc') or '') if isinstance(value, dict) else str(value) for value in benefits]
        values = [value.strip() for value in values if value.strip() and value.casefold() in evidence.casefold()]
        gate = filter_visible_membership_benefits(values, rendered_visibility)
        if gate.accepted:
            verified = list(gate.content)
    # An empty extraction is unknown; it is never evidence of no entitlements.
    complete = bool(verified) and len(verified) == len(benefits or [])
    return {'accepted': True, 'reason': 'validated' if complete else 'membership_benefits_unverified', 'benefits': verified, 'is_offer_extracted': complete, 'complete': complete, 'membership_price': fee}
