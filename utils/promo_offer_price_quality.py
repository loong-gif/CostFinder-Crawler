"""Deterministic audit and repair planning for promo offer price data.

This module intentionally separates a reviewable plan from database writes.
Only price facts explicitly present in ``offer_raw_text`` or exact arithmetic
are updated automatically; everything else becomes a review case.
"""
from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal, InvalidOperation, ROUND_HALF_UP
from typing import Any, Iterable, Mapping

from utils.offer_item_price_repair import build_offer_item_price_plan
from utils.offer_price_evidence import extract_price_evidence


_MONEY = Decimal("0.01")
_PERCENT = Decimal("0.01")
_UNIT = Decimal("0.0001")


@dataclass(frozen=True)
class PriceIssue:
    offer_id: int | None
    rule_name: str
    expected: str
    actual: str
    raw_text_snippet: str
    severity: str = "ERROR"

    def as_row(self) -> dict[str, Any]:
        return {
            "offer_id": self.offer_id,
            "check_category": "促销价格质量",
            "rule_name": self.rule_name,
            "severity": self.severity,
            "expected": self.expected,
            "actual": self.actual,
            "raw_text_snippet": self.raw_text_snippet,
        }


@dataclass(frozen=True)
class PriceAuditReport:
    issues: list[PriceIssue]


@dataclass(frozen=True)
class ReviewCase:
    offer_id: int | None
    rule_name: str
    raw_text: str
    snapshot: dict[str, Any]
    proposed_patch: dict[str, Any]

    def as_row(self) -> dict[str, Any]:
        return {
            "offer_id": self.offer_id,
            "rule_name": self.rule_name,
            "raw_text": self.raw_text,
            "snapshot": self.snapshot,
            "proposed_patch": self.proposed_patch,
            "status": "pending",
        }


@dataclass(frozen=True)
class PriceRepairPlan:
    master_updates: list[dict[str, Any]]
    item_updates: list[dict[str, Any]]
    review_cases: list[ReviewCase]


def _decimal(value: Any) -> Decimal | None:
    if value is None or isinstance(value, bool) or str(value).strip() == "":
        return None
    try:
        return Decimal(str(value).replace(",", "").replace("$", "").strip())
    except (InvalidOperation, ValueError):
        return None


def _positive(value: Any) -> Decimal | None:
    parsed = _decimal(value)
    return parsed if parsed is not None and parsed > 0 else None


def _round(value: Decimal, precision: Decimal) -> Decimal:
    return value.quantize(precision, rounding=ROUND_HALF_UP)


def _shown(value: Decimal | None) -> str:
    return "NULL" if value is None else format(value, "f")


def _different(left: Decimal | None, right: Decimal | None, tolerance: Decimal = _MONEY) -> bool:
    return left is None or right is None or abs(left - right) > tolerance


def _items(row: Mapping[str, Any]) -> list[Mapping[str, Any]]:
    value = row.get("promo_offer_items")
    return value if isinstance(value, list) else []


def _single_logical_service(items: list[Mapping[str, Any]]) -> bool:
    if not items:
        return False
    service_ids = {item.get("service_id") for item in items}
    return None not in service_ids and (len(items) == 1 or len(service_ids) == 1)


def _item_unit_expectation(row: Mapping[str, Any]) -> Decimal | None:
    discount = _positive(row.get("discount_price"))
    items = _items(row)
    model = str(row.get("price_model") or "").strip().lower()
    if discount is None or not _single_logical_service(items):
        return None
    if model == "per_unit":
        return discount
    if model != "total":
        return None
    quantities = [_positive(item.get("quantity")) for item in items]
    if any(quantity is None for quantity in quantities):
        return None
    total_quantity = sum(quantities, Decimal(0))
    if total_quantity <= 0:
        return None
    unit = _round(discount / total_quantity, _UNIT)
    return unit if unit * total_quantity == discount else None


def audit_offer_prices(rows: Iterable[Mapping[str, Any]]) -> PriceAuditReport:
    issues: list[PriceIssue] = []
    for row in rows:
        offer_id = row.get("id")
        raw = str(row.get("offer_raw_text") or "")
        snippet = raw[:300]
        regular = _positive(row.get("regular_price"))
        discount = _positive(row.get("discount_price"))
        amount = _positive(row.get("discount_amount"))
        percent = _positive(row.get("discount_percent"))

        if discount is None and amount is None and percent is None:
            issues.append(PriceIssue(offer_id, "价格全为NULL", "至少一个折扣字段", "均为NULL", snippet))

        if regular is not None and discount is not None and discount < regular:
            expected_amount = _round(regular - discount, _MONEY)
            expected_percent = _round((regular - discount) * Decimal("100") / regular, _PERCENT)
            if amount is not None and _different(amount, expected_amount):
                issues.append(PriceIssue(offer_id, "discount_amount一致性", _shown(expected_amount), _shown(amount), snippet))
            if percent is not None and _different(percent, expected_percent, _PERCENT):
                issues.append(PriceIssue(offer_id, "discount_percent一致性", _shown(expected_percent), _shown(percent), snippet))

        expected_unit = _item_unit_expectation(row)
        if expected_unit is None:
            continue
        model = str(row.get("price_model") or "").strip().lower()
        rule = "per_unit模式: discount_price=unit_price" if model == "per_unit" else "total模式: discount_price=unit_price*qty"
        for item in _items(row):
            actual = _positive(item.get("unit_price"))
            if _different(actual, expected_unit, _UNIT):
                issues.append(PriceIssue(offer_id, rule, _shown(expected_unit), _shown(actual), snippet))
                break
    return PriceAuditReport(issues)


def _master_patch(row: Mapping[str, Any]) -> dict[str, Any]:
    evidence = extract_price_evidence(row.get("offer_raw_text"))
    candidate = {
        field: row.get(field)
        for field in ("regular_price", "discount_price", "discount_amount", "discount_percent")
    }
    evidence_has_price_pair = (
        _positive(evidence.get("regular_price")) is not None
        and _positive(evidence.get("discount_price")) is not None
    )
    if evidence_has_price_pair:
        candidate["regular_price"] = evidence["regular_price"]
        candidate["discount_price"] = evidence["discount_price"]
    else:
        # A lone "only $X" can be a package total rather than a comparable
        # per-service price. Accept it only when the stored counterpart proves
        # that it is a valid price pair; otherwise leave it for review.
        evidence_regular = _positive(evidence.get("regular_price"))
        evidence_discount = _positive(evidence.get("discount_price"))
        current_regular = _positive(candidate["regular_price"])
        current_discount = _positive(candidate["discount_price"])
        if evidence_regular is not None and current_discount is not None and current_discount < evidence_regular:
            candidate["regular_price"] = float(evidence_regular)
        if evidence_discount is not None and current_regular is not None and evidence_discount < current_regular:
            candidate["discount_price"] = float(evidence_discount)

    for field in ("discount_amount", "discount_percent"):
        if field in evidence:
            candidate[field] = evidence[field]

    regular = _positive(candidate["regular_price"])
    discount = _positive(candidate["discount_price"])
    if regular is not None and discount is not None and discount < regular:
        candidate["discount_amount"] = float(_round(regular - discount, _MONEY))
        candidate["discount_percent"] = float(_round((regular - discount) * Decimal("100") / regular, _PERCENT))
    return candidate


def _changed_patch(row: Mapping[str, Any], candidate: Mapping[str, Any]) -> dict[str, Any]:
    patch: dict[str, Any] = {}
    for field, value in candidate.items():
        current = _decimal(row.get(field))
        next_value = _decimal(value)
        if current != next_value:
            patch[field] = float(next_value) if next_value is not None else None
    return patch


def build_price_repair_plan(rows: Iterable[Mapping[str, Any]]) -> PriceRepairPlan:
    row_list = [dict(row) for row in rows]
    initial = audit_offer_prices(row_list)
    initial_by_offer: dict[Any, list[PriceIssue]] = {}
    for issue in initial.issues:
        initial_by_offer.setdefault(issue.offer_id, []).append(issue)

    master_updates: list[dict[str, Any]] = []
    review_cases: list[ReviewCase] = []
    candidate_rows: list[dict[str, Any]] = []
    for row in row_list:
        candidate = dict(row)
        patch = _changed_patch(row, _master_patch(row))
        if patch:
            candidate.update(patch)
            timestamp = row.get("updated_at")
            if timestamp:
                patch = {"id": row.get("id"), **patch, "updated_at": timestamp}
                master_updates.append(patch)
        candidate_rows.append(candidate)

    item_plan = build_offer_item_price_plan(candidate_rows)
    item_updates: list[dict[str, Any]] = []
    timestamps = {
        item.get("offer_item_id"): item.get("updated_at")
        for row in row_list
        for item in _items(row)
        if item.get("offer_item_id") is not None
    }
    for update in item_plan.updates:
        timestamp = timestamps.get(update["offer_item_id"])
        if timestamp:
            item_updates.append({**update, "updated_at": timestamp})

    item_updates_by_id = {update["offer_item_id"]: update["unit_price"] for update in item_plan.updates}
    for row in candidate_rows:
        items = row.get("promo_offer_items")
        if not isinstance(items, list):
            continue
        row["promo_offer_items"] = [
            {**item, "unit_price": item_updates_by_id.get(item.get("offer_item_id"), item.get("unit_price"))}
            for item in items
        ]

    patched_by_id = {row.get("id"): row for row in candidate_rows}
    for offer_id, issues in initial_by_offer.items():
        candidate_issues = audit_offer_prices([patched_by_id[offer_id]]).issues
        remaining = {issue.rule_name for issue in candidate_issues}
        for issue in issues:
            if issue.rule_name not in remaining:
                continue
            row = patched_by_id[offer_id]
            review_cases.append(
                ReviewCase(
                    offer_id=offer_id,
                    rule_name=issue.rule_name,
                    raw_text=str(row.get("offer_raw_text") or ""),
                    snapshot={
                        field: row.get(field)
                        for field in ("regular_price", "discount_price", "discount_amount", "discount_percent", "price_model")
                    },
                    proposed_patch={},
                )
            )
    return PriceRepairPlan(master_updates, item_updates, review_cases)
