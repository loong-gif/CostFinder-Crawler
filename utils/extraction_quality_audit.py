"""Unified read-only QA for clinic extraction tables + raw lineage."""
from __future__ import annotations

import re
from collections import Counter, defaultdict
from dataclasses import dataclass, field
from datetime import date
from typing import Any, Dict, Iterable, List, Mapping, Optional, Sequence, Set, Tuple
from urllib.parse import urlparse

from utils.clinic_service_extraction import is_promo_offer
from utils.service_price_guard import is_catalog_ineligible_url, infer_unit_count
from utils.promo_offer_audit import (
    AuditIssue as OfferAuditIssue,
    audit_rows as audit_offer_rows,
    build_exact_duplicate_groups,
    build_fingerprint_duplicate_groups,
    build_offer_text_duplicate_groups,
    is_valid_url,
    normalize_text,
    parse_float,
)
from utils.schema_contract import offer_is_active, offer_item_name, offer_source_url

TABLES = (
    "clinic_services",
    "clinic_memberships",
    "clinic_promotions",
    "promo_offer_master",
    "promo_offer_items",
)

_COMMITMENT_RE = re.compile(
    r"(?:(\d+)\s*[- ]?(?:month|mo)\b.*?(?:minimum|commitment|contract|required))"
    r"|(?:(?:minimum|commitment|contract).*?(\d+)\s*[- ]?(?:month|mo))",
    re.IGNORECASE,
)
_YEAR_COMMITMENT_RE = re.compile(
    r"(?:1\s*[- ]?year|one\s+year|12\s*[- ]?month).*(?:minimum|commitment|contract|required)",
    re.IGNORECASE,
)
_DERIVED_PERCENT_RE = re.compile(r"\d+(?:\.\d+)?\s*%")


@dataclass
class TableAuditIssue:
    table: str
    row_id: Any
    severity: str
    issue_type: str
    detail: str
    business_name: str = ""
    label: str = ""

    def as_row(self) -> Dict[str, Any]:
        return {
            "table": self.table,
            "id": self.row_id,
            "severity": self.severity,
            "issue_type": self.issue_type,
            "detail": self.detail,
            "business_name": self.business_name,
            "label": self.label,
        }


@dataclass
class AuditReport:
    issues: List[TableAuditIssue] = field(default_factory=list)
    table_counts: Dict[str, int] = field(default_factory=dict)
    summary: Dict[str, Any] = field(default_factory=dict)
    duplicate_groups: Dict[str, List[Dict[str, Any]]] = field(default_factory=dict)

    @property
    def blocking_count(self) -> int:
        return sum(1 for issue in self.issues if issue.severity == "high")

    def as_summary(self) -> Dict[str, Any]:
        by_table = Counter(issue.table for issue in self.issues)
        by_type = Counter(issue.issue_type for issue in self.issues)
        high = Counter(
            issue.issue_type for issue in self.issues if issue.severity == "high"
        )
        return {
            "table_counts": self.table_counts,
            "total_issues": len(self.issues),
            "blocking_issues": self.blocking_count,
            "issues_by_table": dict(by_table),
            "issues_by_type": dict(by_type),
            "high_severity_by_type": dict(high),
            **self.summary,
        }


def _norm_url(value: Any) -> str:
    return str(value or "").strip().lower().rstrip("/")


def _business_name(business_id: Any, lookup: Mapping[Any, Mapping[str, Any]]) -> str:
    row = lookup.get(business_id) or {}
    return str(row.get("name") or row.get("business_name") or "").strip()


def _add(
    issues: List[TableAuditIssue],
    *,
    table: str,
    row_id: Any,
    severity: str,
    issue_type: str,
    detail: str,
    business_name: str = "",
    label: str = "",
) -> None:
    issues.append(
        TableAuditIssue(
            table=table,
            row_id=row_id,
            severity=severity,
            issue_type=issue_type,
            detail=detail,
            business_name=business_name,
            label=label,
        )
    )


def _infer_commitment_months(benefits: Sequence[str]) -> Optional[int]:
    text = " ".join(str(value) for value in benefits)
    if _YEAR_COMMITMENT_RE.search(text):
        return 12
    match = _COMMITMENT_RE.search(text)
    if not match:
        return None
    for group in match.groups():
        if group:
            return int(group)
    return None


def audit_services(
    rows: Sequence[Mapping[str, Any]],
    *,
    business_lookup: Mapping[Any, Mapping[str, Any]],
    scrape_urls: Set[str],
    search_urls: Set[str],
) -> List[TableAuditIssue]:
    issues: List[TableAuditIssue] = []
    for row in rows:
        sid = row.get("service_id")
        biz = row.get("business_id")
        name = str(row.get("service_name") or "").strip()
        biz_name = _business_name(biz, business_lookup)
        if name == "Others":
            _add(
                issues,
                table="clinic_services",
                row_id=sid,
                severity="medium",
                issue_type="generic_service_name",
                detail="service_name=Others",
                business_name=biz_name,
                label=name,
            )
        if not str(row.get("service_name_raw") or "").strip():
            _add(
                issues,
                table="clinic_services",
                row_id=sid,
                severity="low",
                issue_type="missing_service_name_raw",
                detail="service_name_raw 为空",
                business_name=biz_name,
                label=name,
            )
        source_url = _norm_url(row.get("source_url"))
        if source_url and is_catalog_ineligible_url(row.get("source_url") or ""):
            _add(
                issues,
                table="clinic_services",
                row_id=sid,
                severity="high",
                issue_type="ineligible_catalog_source",
                detail=f"博客/促销来源写入服务目录: {source_url}",
                business_name=biz_name,
                label=name,
            )
        if source_url and source_url not in scrape_urls and source_url not in search_urls:
            _add(
                issues,
                table="clinic_services",
                row_id=sid,
                severity="low",
                issue_type="missing_raw_lineage",
                detail=f"source_url 未匹配 raw: {source_url}",
                business_name=biz_name,
                label=name,
            )
        price = parse_float(row.get("regular_price"))
        unit_type = str(row.get("unit_type") or "").lower()
        if price is not None and price > 0 and unit_type in {"session", "area", "treatment", "package"}:
            count, upper = infer_unit_count(str(row.get("service_name_raw") or ""), price)
            if count is not None and count >= 2:
                _add(
                    issues,
                    table="clinic_services",
                    row_id=sid,
                    severity="high",
                    issue_type="package_price_not_normalized",
                    detail=f"{price} 疑似 {count} 单位套餐价"
                    + (" (up to)" if upper else ""),
                    business_name=biz_name,
                    label=name,
                )
        if price is not None and price <= 0:
            _add(
                issues,
                table="clinic_services",
                row_id=sid,
                severity="high",
                issue_type="invalid_regular_price",
                detail=f"regular_price={row.get('regular_price')}",
                business_name=biz_name,
                label=name,
            )
        elif price is None:
            _add(
                issues,
                table="clinic_services",
                row_id=sid,
                severity="low",
                issue_type="missing_catalog_price",
                detail="regular_price 为空，等待常规目录价回填",
                business_name=biz_name,
                label=name,
            )
    return issues


def audit_memberships(
    rows: Sequence[Mapping[str, Any]],
    *,
    business_lookup: Mapping[Any, Mapping[str, Any]],
) -> List[TableAuditIssue]:
    issues: List[TableAuditIssue] = []
    for row in rows:
        pid = row.get("plan_id")
        biz_name = _business_name(row.get("business_id"), business_lookup)
        benefits = row.get("benefits") or []
        if isinstance(benefits, str):
            benefits = [benefits]
        if not benefits:
            _add(
                issues,
                table="clinic_memberships",
                row_id=pid,
                severity="high",
                issue_type="missing_benefits",
                detail="benefits 为空",
                business_name=biz_name,
                label=str(row.get("membership_name") or ""),
            )
        inferred = _infer_commitment_months(benefits)
        current = row.get("minimum_commitment_months")
        if inferred and not current:
            _add(
                issues,
                table="clinic_memberships",
                row_id=pid,
                severity="medium",
                issue_type="missing_commitment_months",
                detail=f"benefits 暗示 {inferred} 个月承诺期",
                business_name=biz_name,
                label=str(row.get("membership_name") or ""),
            )
        if not str(row.get("source_url") or "").strip():
            _add(
                issues,
                table="clinic_memberships",
                row_id=pid,
                severity="medium",
                issue_type="missing_source_url",
                detail="source_url 为空",
                business_name=biz_name,
                label=str(row.get("membership_name") or ""),
            )
    return issues


def audit_promotions(
    rows: Sequence[Mapping[str, Any]],
    *,
    business_lookup: Mapping[Any, Mapping[str, Any]],
) -> List[TableAuditIssue]:
    issues: List[TableAuditIssue] = []
    url_groups: Dict[Tuple[Any, str], List[Mapping[str, Any]]] = defaultdict(list)
    for row in rows:
        pid = row.get("promotion_id")
        biz = row.get("business_id")
        biz_name = _business_name(biz, business_lookup)
        title = str(row.get("promotion_title") or "").strip()
        source_url = str(row.get("source_url") or "").strip()
        content = row.get("promotion_content") or []
        if not content:
            _add(
                issues,
                table="clinic_promotions",
                row_id=pid,
                severity="medium",
                issue_type="missing_promotion_content",
                detail="promotion_content 为空",
                business_name=biz_name,
                label=title,
            )
        if not source_url or not is_valid_url(source_url):
            _add(
                issues,
                table="clinic_promotions",
                row_id=pid,
                severity="high",
                issue_type="invalid_source_url",
                detail=f"source_url={source_url or '<empty>'}",
                business_name=biz_name,
                label=title,
            )
        if "/membership" in source_url.lower() or source_url.lower().endswith("/memberships"):
            _add(
                issues,
                table="clinic_promotions",
                row_id=pid,
                severity="low",
                issue_type="membership_page_as_promotion",
                detail="membership/pricing 页面被当作活动锚点",
                business_name=biz_name,
                label=title,
            )
        start = str(row.get("campaign_start_date") or "").strip()
        end = str(row.get("campaign_end_date") or "").strip()
        if start and end and end < start:
            _add(
                issues,
                table="clinic_promotions",
                row_id=pid,
                severity="high",
                issue_type="end_before_start",
                detail=f"{start} > {end}",
                business_name=biz_name,
                label=title,
            )
        url_groups[(biz, _norm_url(source_url))].append(row)
    for key, group in url_groups.items():
        if len(group) > 1:
            _add(
                issues,
                table="clinic_promotions",
                row_id=",".join(str(row.get("promotion_id")) for row in group),
                severity="medium",
                issue_type="duplicate_promotion_url",
                detail=f"同 business+URL 有 {len(group)} 条 promotion",
                business_name=_business_name(key[0], business_lookup),
                label=str(group[0].get("promotion_title") or ""),
            )
    return issues


def audit_offer_items(
    rows: Sequence[Mapping[str, Any]],
    *,
    offer_lookup: Mapping[Any, Mapping[str, Any]],
    service_lookup: Mapping[Any, Mapping[str, Any]],
    business_lookup: Mapping[Any, Mapping[str, Any]],
) -> List[TableAuditIssue]:
    issues: List[TableAuditIssue] = []
    for row in rows:
        item_id = row.get("offer_item_id")
        offer_id = row.get("offer_id")
        offer = offer_lookup.get(offer_id) or {}
        biz = offer.get("business_id")
        biz_name = _business_name(biz, business_lookup)
        service_id = row.get("service_id")
        if service_id is None and offer_is_active(dict(offer)):
            _add(
                issues,
                table="promo_offer_items",
                row_id=item_id,
                severity="medium",
                issue_type="missing_service_link",
                detail=f"active offer {offer_id} item 未关联 service",
                business_name=biz_name,
                label=str(offer.get("offer_raw_text") or "")[:80],
            )
        if service_id is not None:
            svc = service_lookup.get(service_id)
            if not svc:
                _add(
                    issues,
                    table="promo_offer_items",
                    row_id=item_id,
                    severity="high",
                    issue_type="orphan_service_id",
                    detail=f"service_id={service_id} 不存在",
                    business_name=biz_name,
                )
            elif svc.get("business_id") != biz:
                _add(
                    issues,
                    table="promo_offer_items",
                    row_id=item_id,
                    severity="high",
                    issue_type="service_business_mismatch",
                    detail="item service 与 offer business 不一致",
                    business_name=biz_name,
                )
    return issues


def audit_offers_live(
    rows: Sequence[Mapping[str, Any]],
    *,
    master_business_ids: Set[Any],
    membership_plan_ids: Set[Any],
    service_lookup: Mapping[Any, Mapping[str, Any]],
    business_lookup: Mapping[Any, Mapping[str, Any]],
    today: Optional[date] = None,
) -> List[TableAuditIssue]:
    issues: List[TableAuditIssue] = []
    offer_issues, _, _ = audit_offer_rows(
        rows,
        master_business_ids=master_business_ids,
        membership_plan_ids=membership_plan_ids,
        service_rows=service_lookup,
        today=today,
    )
    for issue in offer_issues:
        row = next((item for item in rows if item.get("id") == issue.id), {})
        biz_name = _business_name(row.get("business_id"), business_lookup)
        issues.append(
            TableAuditIssue(
                table="promo_offer_master",
                row_id=issue.id,
                severity=issue.severity,
                issue_type=issue.issue_type,
                detail=issue.detail,
                business_name=biz_name,
                label=issue.service_name,
            )
        )
    for row in rows:
        if not offer_is_active(dict(row)):
            continue
        row_id = row.get("id")
        biz_name = _business_name(row.get("business_id"), business_lookup)
        if not is_promo_offer(dict(row)):
            _add(
                issues,
                table="promo_offer_master",
                row_id=row_id,
                severity="high",
                issue_type="non_promo_in_master",
                detail="无 discount 字段，应路由到 clinic_services",
                business_name=biz_name,
                label=offer_item_name(dict(row), service_lookup=service_lookup),
            )
        regular = parse_float(row.get("regular_price"))
        discount = parse_float(row.get("discount_price"))
        if regular is not None and discount is not None and abs(regular - discount) < 0.01:
            _add(
                issues,
                table="promo_offer_master",
                row_id=row_id,
                severity="medium",
                issue_type="discount_equals_regular",
                detail=f"discount_price=regular_price={regular}",
                business_name=biz_name,
                label=str(row.get("offer_raw_text") or "")[:80],
            )
        raw = str(row.get("offer_raw_text") or "")
        if row.get("discount_percent") is not None and not _DERIVED_PERCENT_RE.search(raw):
            _add(
                issues,
                table="promo_offer_master",
                row_id=row_id,
                severity="medium",
                issue_type="derived_discount_percent",
                detail="discount_percent 无原文百分比证据",
                business_name=biz_name,
                label=raw[:80],
            )
        fp = str(row.get("offer_fingerprint") or "")
        if fp and len(fp) == 32:
            _add(
                issues,
                table="promo_offer_master",
                row_id=row_id,
                severity="low",
                issue_type="legacy_fingerprint_format",
                detail="32 位 fingerprint，应统一为 sha1",
                business_name=biz_name,
            )
        if re.search(r"\$\d+(?:\.\d+)?\s*per month", raw, re.I):
            _add(
                issues,
                table="promo_offer_master",
                row_id=row_id,
                severity="high",
                issue_type="membership_fee_in_promo",
                detail="疑似 membership 月费误入 promo",
                business_name=biz_name,
                label=raw[:80],
            )
    return issues


def audit_raw_lineage(
    *,
    services: Sequence[Mapping[str, Any]],
    memberships: Sequence[Mapping[str, Any]],
    promotions: Sequence[Mapping[str, Any]],
    scrape_urls: Set[str],
    search_urls: Set[str],
    business_lookup: Mapping[Any, Mapping[str, Any]],
) -> List[TableAuditIssue]:
    issues: List[TableAuditIssue] = []
    all_urls = scrape_urls | search_urls
    for table, rows, id_field in (
        ("clinic_services", services, "service_id"),
        ("clinic_memberships", memberships, "plan_id"),
        ("clinic_promotions", promotions, "promotion_id"),
    ):
        for row in rows:
            url = _norm_url(row.get("source_url"))
            if not url:
                continue
            if url in all_urls:
                continue
            _add(
                issues,
                table=table,
                row_id=row.get(id_field),
                severity="low",
                issue_type="source_url_not_in_raw",
                detail=f"source_url 未匹配 firecrawl raw: {url}",
                business_name=_business_name(row.get("business_id"), business_lookup),
            )
    return issues


def run_full_audit(
    *,
    services: Sequence[Mapping[str, Any]],
    memberships: Sequence[Mapping[str, Any]],
    promotions: Sequence[Mapping[str, Any]],
    offers: Sequence[Mapping[str, Any]],
    offer_items: Sequence[Mapping[str, Any]],
    businesses: Sequence[Mapping[str, Any]],
    scrape_urls: Sequence[str],
    search_urls: Sequence[str],
    today: Optional[date] = None,
) -> AuditReport:
    business_lookup = {row["business_id"]: row for row in businesses if row.get("business_id") is not None}
    service_lookup = {
        row["service_id"]: row for row in services if row.get("service_id") is not None
    }
    offer_lookup = {row["id"]: row for row in offers if row.get("id") is not None}
    master_ids = set(business_lookup)
    plan_ids = {row["plan_id"] for row in memberships if row.get("plan_id") is not None}
    scrape_set = {_norm_url(url) for url in scrape_urls}
    search_set = {_norm_url(url) for url in search_urls}

    issues: List[TableAuditIssue] = []
    issues.extend(
        audit_services(
            services,
            business_lookup=business_lookup,
            scrape_urls=scrape_set,
            search_urls=search_set,
        )
    )
    issues.extend(
        audit_memberships(rows=memberships, business_lookup=business_lookup)
    )
    issues.extend(
        audit_promotions(rows=promotions, business_lookup=business_lookup)
    )
    issues.extend(
        audit_offers_live(
            offers,
            master_business_ids=master_ids,
            membership_plan_ids=plan_ids,
            service_lookup=service_lookup,
            business_lookup=business_lookup,
            today=today,
        )
    )
    issues.extend(
        audit_offer_items(
            offer_items,
            offer_lookup=offer_lookup,
            service_lookup=service_lookup,
            business_lookup=business_lookup,
        )
    )
    issues.extend(
        audit_raw_lineage(
            services=services,
            memberships=memberships,
            promotions=promotions,
            scrape_urls=scrape_set,
            search_urls=search_set,
            business_lookup=business_lookup,
        )
    )

    exact = build_exact_duplicate_groups(offers)
    text_dup = build_offer_text_duplicate_groups(offers)
    fp_dup = build_fingerprint_duplicate_groups(offers)
    report = AuditReport(
        issues=issues,
        table_counts={
            "clinic_services": len(services),
            "clinic_memberships": len(memberships),
            "clinic_promotions": len(promotions),
            "promo_offer_master": len(offers),
            "promo_offer_items": len(offer_items),
        },
        duplicate_groups={
            "exact": exact,
            "offer_text": text_dup,
            "fingerprint": fp_dup,
        },
        summary={
            "raw_scrape_urls": len(scrape_set),
            "raw_search_urls": len(search_set),
        },
    )
    report.summary.update(report.as_summary())
    return report
