"""Schema-aware QA rules for promo_offer_master (live Supabase)."""
from __future__ import annotations

import re
from collections import Counter, defaultdict
from dataclasses import dataclass
from datetime import date
from typing import Any, Dict, Iterable, List, Mapping, Optional, Sequence, Set, Tuple
from urllib.parse import urlparse

from utils.align_service_names import infer_alignment
from utils.offer_scope_filter import exclude_reason, should_exclude_from_offer_master
from utils.schema_contract import OFFER_MASTER_COLUMNS, offer_is_active, offer_item_name, offer_source_url, offer_unit_type

# Live columns (aligned to current Supabase DDL)
LIVE_OFFER_COLUMNS: Tuple[str, ...] = OFFER_MASTER_COLUMNS + (
    "promo_offer_items(offer_item_id,service_id,quantity,unit_price)",
    "clinic_promotions(source_url,promotion_title,campaign_start_date,campaign_end_date)",
)

EXACT_DUPLICATE_FIELDS: Tuple[str, ...] = (
    "business_id",
    "promotion_id",
    "regular_price",
    "discount_price",
    "discount_amount",
    "discount_percent",
    "is_membership_required",
    "offer_raw_text",
    "offer_fingerprint",
)

BY_UNIT_PATTERNS: Tuple[Tuple[str, re.Pattern[str]], ...] = (
    (
        "unit",
        re.compile(
            r"(?:\$?\s*\d+(?:\.\d+)?\s*(?:/|per)\s*unit\b|\bper unit\b|\b\d+\s+units?\b)",
            re.IGNORECASE,
        ),
    ),
    (
        "syringe",
        re.compile(
            r"(?:\$?\s*\d+(?:\.\d+)?\s*(?:/|per)\s*(?:syringe|half syringe)\b|\bhalf syringe\b|\bsyringe\b)",
            re.IGNORECASE,
        ),
    ),
)

VALID_STATUSES = frozenset({"active", "ended"})  # legacy audit display only


def _row_status_label(row: Mapping[str, Any]) -> str:
    return "active" if offer_is_active(dict(row)) else "ended"


def _item_service_ids(row: Mapping[str, Any]) -> List[Any]:
    items = row.get("promo_offer_items")
    if isinstance(items, list):
        return [item.get("service_id") for item in items if item.get("service_id") is not None]
    if isinstance(items, dict) and items.get("service_id") is not None:
        return [items["service_id"]]
    legacy = row.get("service_id")
    return [legacy] if legacy is not None else []


@dataclass
class AuditIssue:
    id: Any
    status: str
    source_name: str
    service_name: str
    issue_type: str
    severity: str
    detail: str

    def as_row(self) -> Dict[str, Any]:
        return {
            "id": self.id,
            "status": self.status,
            "source_name": self.source_name,
            "service_name": self.service_name,
            "issue_type": self.issue_type,
            "severity": self.severity,
            "detail": self.detail,
        }


def normalize_text(value: Any) -> str:
    text = str(value or "").strip().lower()
    text = text.replace("'", "'").replace(""", '"').replace(""", '"')
    text = text.replace("®", "").replace("™", "")
    return re.sub(r"\s+", " ", text)


def normalize_offer_raw_text(value: Any) -> str:
    text = normalize_text(value)
    text = re.sub(r"https?://\S+", "", text)
    return re.sub(r"\s+", " ", text).strip(" -;,.")


def normalize_value(value: Any) -> str:
    if value is None:
        return ""
    if isinstance(value, bool):
        return "true" if value else "false"
    return str(value).strip()


def parse_float(value: Any) -> Optional[float]:
    raw = str(value or "").strip().replace(",", "")
    if not raw:
        return None
    try:
        return float(raw)
    except ValueError:
        return None


def parse_bool(value: Any) -> Optional[bool]:
    raw = normalize_text(value)
    if raw in {"true", "t", "1", "yes"}:
        return True
    if raw in {"false", "f", "0", "no"}:
        return False
    return None


def is_valid_url(value: Any) -> bool:
    try:
        parsed = urlparse(str(value or ""))
        return parsed.scheme in {"http", "https"} and bool(parsed.netloc)
    except Exception:
        return False


def detect_likely_basis(row: Mapping[str, Any]) -> List[str]:
    row_dict = dict(row)
    text = " ".join(
        str(row_dict.get(field) or "")
        for field in ("offer_raw_text",)
    )
    text = f"{offer_item_name(row_dict)} {offer_unit_type(row_dict)} {text}"
    return [label for label, pattern in BY_UNIT_PATTERNS if pattern.search(text)]


def build_exact_duplicate_groups(rows: Sequence[Mapping[str, Any]]) -> List[Dict[str, Any]]:
    groups: Dict[Tuple[str, ...], List[Mapping[str, Any]]] = defaultdict(list)
    for row in rows:
        key = tuple(normalize_value(row.get(field)) for field in EXACT_DUPLICATE_FIELDS)
        groups[key].append(row)

    out: List[Dict[str, Any]] = []
    for group_rows in groups.values():
        if len(group_rows) < 2:
            continue
        out.append(
            {
                "count": len(group_rows),
                "ids": [row.get("id") for row in group_rows],
                "source_name": group_rows[0].get("source_name", ""),
                "service_name": offer_item_name(dict(group_rows[0])),
                "offer_raw_text": group_rows[0].get("offer_raw_text", ""),
            }
        )
    out.sort(key=lambda item: (-item["count"], str(item["ids"][0])))
    return out


def build_offer_text_duplicate_groups(rows: Sequence[Mapping[str, Any]]) -> List[Dict[str, Any]]:
    groups: Dict[Tuple[str, str], List[Mapping[str, Any]]] = defaultdict(list)
    for row in rows:
        source_name = normalize_text(row.get("source_name"))
        normalized_offer = normalize_offer_raw_text(row.get("offer_raw_text"))
        if not source_name or not normalized_offer:
            continue
        groups[(source_name, normalized_offer)].append(row)

    duplicates: List[Dict[str, Any]] = []
    for (source_name, normalized_offer), group_rows in groups.items():
        if len(group_rows) < 2:
            continue
        service_names = sorted(
            {
                offer_item_name(dict(item))
                for item in group_rows
                if offer_item_name(dict(item))
            }
        )
        duplicates.append(
            {
                "count": len(group_rows),
                "source_name": source_name,
                "normalized_offer_raw_text": normalized_offer,
                "service_names": service_names,
                "ids": [item.get("id") for item in group_rows],
                "active_count": sum(offer_is_active(dict(item)) for item in group_rows),
            }
        )
    duplicates.sort(
        key=lambda item: (-item["count"], item["source_name"], item["normalized_offer_raw_text"])
    )
    return duplicates


def build_fingerprint_duplicate_groups(rows: Sequence[Mapping[str, Any]]) -> List[Dict[str, Any]]:
    groups: Dict[str, List[Mapping[str, Any]]] = defaultdict(list)
    for row in rows:
        fp = str(row.get("offer_fingerprint") or "").strip()
        if not fp or not offer_is_active(dict(row)):
            continue
        groups[fp].append(row)

    out: List[Dict[str, Any]] = []
    for fp, group_rows in groups.items():
        if len(group_rows) < 2:
            continue
        out.append(
            {
                "offer_fingerprint": fp,
                "count": len(group_rows),
                "ids": [row.get("id") for row in group_rows],
            }
        )
    out.sort(key=lambda item: (-item["count"], item["offer_fingerprint"]))
    return out


def audit_rows(
    rows: Sequence[Mapping[str, Any]],
    *,
    master_business_ids: Optional[Set[Any]] = None,
    membership_plan_ids: Optional[Set[Any]] = None,
    service_rows: Optional[Mapping[Any, Mapping[str, Any]]] = None,
    today: Optional[date] = None,
) -> Tuple[List[AuditIssue], List[Dict[str, Any]], Dict[str, Any]]:
    """Return issues, alignment rows, and layered summary stats."""
    issues: List[AuditIssue] = []
    alignment_rows: List[Dict[str, Any]] = []
    today = today or date.today()
    master_ids = master_business_ids or set()
    plan_ids = membership_plan_ids or set()
    svc_by_id = service_rows or {}

    for row in rows:
        row_id = row.get("id")
        row_dict = dict(row)
        status = _row_status_label(row)
        promo = row_dict.get("clinic_promotions") or {}
        if isinstance(promo, list):
            promo = promo[0] if promo else {}
        source_name = str(
            promo.get("promotion_title")
            or row_dict.get("source_name")
            or row_dict.get("business_name")
            or ""
        ).strip()
        service_name = offer_item_name(row_dict)
        offer_raw_text = str(row.get("offer_raw_text") or "").strip()
        source_url = offer_source_url(row_dict)
        regular_price = parse_float(row.get("regular_price"))
        discount_price = parse_float(row.get("discount_price"))
        discount_amount = parse_float(row.get("discount_amount"))
        discount_percent = parse_float(row.get("discount_percent"))
        membership_plan_id = row.get("membership_plan_id")
        promotion_id = row.get("promotion_id")
        business_id = row.get("business_id")
        is_active = offer_is_active(row_dict)
        item_service_ids = _item_service_ids(row)

        def add(issue_type: str, severity: str, detail: str) -> None:
            issues.append(
                AuditIssue(
                    row_id,
                    status,
                    source_name,
                    service_name,
                    issue_type,
                    severity,
                    detail,
                )
            )

        alignment = infer_alignment(service_name, "")
        canonical_name = alignment.get("aligned_service_name_canonical", "")
        alignment_rows.append(
            {
                "id": row_id,
                "status": status,
                "source_name": source_name,
                "service_name": service_name,
                "aligned_service_name_canonical": canonical_name,
                "aligned_service_category": alignment.get("aligned_service_category", ""),
                "needs_manual_review": alignment.get("needs_manual_review", ""),
                "alignment_confidence": alignment.get("alignment_confidence", ""),
                "alignment_note": alignment.get("alignment_note", ""),
            }
        )

        # --- scope / business semantics ---
        scope_reason = exclude_reason(dict(row))
        if scope_reason == "membership_plan" and not membership_plan_id:
            add(
                "scope_membership_plan",
                "high" if is_active else "low",
                "纯会员档位/会费，不属于治疗 SKU offer",
            )
        elif scope_reason == "consultation":
            add("scope_consultation", "high" if is_active else "low", "咨询类，不属于治疗促销 offer")
        elif scope_reason == "skincare_product":
            add(
                "scope_skincare_product",
                "medium" if is_active else "low",
                "疑似零售商品（需人工复核，历史规则有误报）",
            )

        # --- required fields (active = blocking) ---
        if not source_name:
            add("missing_source_name", "high" if is_active else "medium", "source_name 为空")
        if not service_name and not offer_raw_text:
            add("missing_service_name", "high" if is_active else "medium", "service_name 与 offer_raw_text 均为空")
        elif not service_name and is_active and len(offer_raw_text) < 12:
            add("missing_service_name", "medium", "无法从 offer_raw_text 推断服务名")
        if not offer_raw_text:
            add("missing_offer_raw_text", "high" if is_active else "medium", "offer_raw_text 为空")
        if not source_url:
            add("missing_source_url", "high" if is_active else "medium", "promotion source_url 为空")
        elif not is_valid_url(source_url):
            add("invalid_source_url", "high" if is_active else "medium", "source_url 无效")

        if is_active and business_id is None:
            add("missing_business_id", "high", "active offer 缺少 business_id")
        elif business_id is not None and master_ids and business_id not in master_ids:
            add("orphan_business_id", "high", f"business_id={business_id} 不存在于 master")

        if is_active and not str(row.get("offer_fingerprint") or "").strip():
            add("missing_offer_fingerprint", "high", "active offer 缺少 offer_fingerprint")

        # --- FK integrity ---
        if membership_plan_id is not None and plan_ids and membership_plan_id not in plan_ids:
            add("orphan_membership_plan_id", "high", f"membership_plan_id={membership_plan_id} 不存在")
        for service_id in item_service_ids:
            if svc_by_id and service_id not in svc_by_id:
                add("orphan_service_id", "high", f"promo_offer_items.service_id={service_id} 不存在")
            elif (
                business_id is not None
                and service_id in svc_by_id
                and svc_by_id[service_id].get("business_id") != business_id
            ):
                add("service_business_mismatch", "high", "item service_id 与 business_id 不匹配")
        if is_active and promotion_id is None:
            add("missing_promotion_id", "medium", "active offer 缺少 promotion_id")

        # --- lifecycle ---
        end_date = str(promo.get("campaign_end_date") or row.get("end_date") or "").strip()
        start_date = str(promo.get("campaign_start_date") or row.get("start_date") or "").strip()
        if is_active and end_date and end_date < today.isoformat():
            add("active_past_end_date", "high", f"active 但 end_date={end_date} 已过期")
        if start_date and end_date and end_date < start_date:
            add("end_before_start", "high", f"end_date={end_date} < start_date={start_date}")

        # --- pricing ---
        if regular_price is not None and discount_price is not None and discount_price > regular_price:
            add(
                "discount_price_gt_regular_price",
                "high" if is_active else "medium",
                f"discount_price={discount_price} > regular_price={regular_price}",
            )
        for label, value in (
            ("regular_price", regular_price),
            ("discount_price", discount_price),
            ("discount_amount", discount_amount),
            ("discount_percent", discount_percent),
        ):
            if value is not None and value < 0:
                add("negative_price_field", "high", f"{label}={value}")
            if is_active and value == 0:
                add("zero_price_field", "medium", f"active {label}=0")
        if discount_percent is not None and discount_percent > 100:
            add("discount_percent_gt_100", "high", f"discount_percent={discount_percent}")

        # --- text quality ---
        if offer_raw_text and len(offer_raw_text) < 20:
            add(
                "short_offer_raw_text",
                "medium" if is_active else "low",
                f"offer_raw_text 仅 {len(offer_raw_text)} 字符",
            )

        # --- service name alignment (informational) ---
        if (
            is_active
            and alignment.get("needs_manual_review") == "TRUE"
            and service_name not in {"", "Package"}
        ):
            add(
                "service_name_manual_review",
                "medium",
                alignment.get("alignment_note", "service_name 需人工复核"),
            )
        elif is_active and canonical_name and canonical_name != service_name and service_name != "Package":
            add(
                "service_name_noncanonical",
                "low",
                f"建议标准化为 {canonical_name}",
            )

        if is_active and should_exclude_from_offer_master(dict(row)):
            add("active_out_of_scope", "high", f"active 但应排除: {scope_reason or 'out_of_scope'}")

    summary = summarize_issues(issues, rows)
    return issues, alignment_rows, summary


def summarize_issues(
    issues: Sequence[AuditIssue],
    rows: Sequence[Mapping[str, Any]],
) -> Dict[str, Any]:
    issue_counter = Counter(issue.issue_type for issue in issues)
    active_issues = [issue for issue in issues if issue.status == "active"]
    active_counter = Counter(issue.issue_type for issue in active_issues)
    high_active = [issue for issue in active_issues if issue.severity == "high"]

    status_counts = Counter(_row_status_label(row) for row in rows)
    return {
        "row_count": len(rows),
        "status_counts": dict(status_counts),
        "total_issues": len(issues),
        "active_issue_count": len(active_issues),
        "active_high_severity_count": len(high_active),
        "issue_type_counts": dict(issue_counter),
        "active_issue_type_counts": dict(active_counter),
        "active_high_issue_types": dict(Counter(issue.issue_type for issue in high_active)),
    }


def is_high_confidence_skincare_product(offer: Mapping[str, Any]) -> bool:
    """Stricter skincare gate for historical cleanup (avoid treatment false positives)."""
    service = str(offer.get("service_name") or "").strip()
    if service == "Skincare Product":
        return True
    category = normalize_text(offer.get("service_category"))
    if category in {"skincare product", "retail"}:
        return True
    raw = normalize_text(offer.get("offer_raw_text"))
    if re.search(r"\b(serum|cleanser|moisturizer|spf|sunscreen|cream|lotion)\b", raw):
        if not re.search(
            r"\b(botox|dysport|filler|tox|unit|syringe|microneedling|laser|hydrafacial)\b",
            raw,
        ):
            return True
    return False
