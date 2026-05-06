#!/usr/bin/env python3
"""
Audit promo_offer_master data quality from Supabase.
"""
from __future__ import annotations

import argparse
import csv
import json
import os
import re
import sys
from collections import Counter, defaultdict
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Sequence, Tuple

import requests
from dotenv import load_dotenv

PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from align_service_names import infer_alignment  # noqa: E402
from config.settings import OUTPUT_ENCODING  # noqa: E402


TABLE_NAME = "promo_offer_master"
DEFAULT_REPORT_DIR = PROJECT_ROOT / "reports"
DEFAULT_RESULT_DIR = PROJECT_ROOT / "output" / "results"
PAGE_SIZE = 1000
META_COLUMNS = {"id", "created_at", "updated_at"}
EXACT_DUPLICATE_FIELDS = [
    "source_name",
    "source_url",
    "service_category",
    "service_name",
    "template_type",
    "offer_raw_text",
    "offer_content",
    "original_price",
    "discount_price",
    "discount_amount",
    "discount_percent",
    "membership_name",
    "membership_price",
    "billing_period",
    "minimum_term",
    "unit_type",
    "min_unit",
    "delivered_unit",
    "start_date",
    "end_date",
    "eligibility",
    "is_package",
    "is_membership_required",
]

BY_UNIT_PATTERNS = [
    ("unit", re.compile(r"(?:\$?\s*\d+(?:\.\d+)?\s*(?:/|per)\s*unit\b|\bper unit\b|\b\d+\s+units?\b)", re.IGNORECASE)),
    ("syringe", re.compile(r"(?:\$?\s*\d+(?:\.\d+)?\s*(?:/|per)\s*(?:syringe|half syringe)\b|\bhalf syringe\b|\bsyringe\b)", re.IGNORECASE)),
    ("area", re.compile(r"(?:\$?\s*\d+(?:\.\d+)?\s*(?:/|per)\s*area\b|\b\d+\s+areas?\b)", re.IGNORECASE)),
]


class SupabaseRestClient:
    def __init__(self, base_url: str, service_role_key: str):
        self.base_url = base_url.rstrip("/") + "/rest/v1"
        self.session = requests.Session()
        self.session.headers.update(
            {
                "apikey": service_role_key,
                "Authorization": f"Bearer {service_role_key}",
                "Accept": "application/json",
                "Content-Type": "application/json",
            }
        )

    def fetch_rows(
        self,
        table: str,
        select: str,
        *,
        filters: Optional[Dict[str, str]] = None,
        limit: Optional[int] = None,
        offset: Optional[int] = None,
        order: Optional[str] = None,
    ) -> List[Dict[str, Any]]:
        params: Dict[str, str] = {"select": select}
        if filters:
            params.update(filters)
        if limit is not None:
            params["limit"] = str(limit)
        if offset is not None:
            params["offset"] = str(offset)
        if order:
            params["order"] = order
        response = self.session.get(f"{self.base_url}/{table}", params=params, timeout=60)
        response.raise_for_status()
        return response.json()


@dataclass
class Issue:
    id: Any
    source_name: str
    service_name: str
    issue_type: str
    severity: str
    detail: str

    def as_row(self) -> Dict[str, Any]:
        return {
            "id": self.id,
            "source_name": self.source_name,
            "service_name": self.service_name,
            "issue_type": self.issue_type,
            "severity": self.severity,
            "detail": self.detail,
        }


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Audit promo_offer_master data quality")
    parser.add_argument("--limit", type=int, default=None, help="只拉取前 N 条记录用于快速检查")
    parser.add_argument("--report-dir", default=str(DEFAULT_REPORT_DIR), help="报告输出目录")
    parser.add_argument("--result-dir", default=str(DEFAULT_RESULT_DIR), help="结构化结果输出目录")
    return parser.parse_args()


def load_supabase_client() -> SupabaseRestClient:
    load_dotenv(PROJECT_ROOT / ".env")
    base_url = os.getenv("SUPABASE_URL")
    service_role_key = os.getenv("SUPABASE_SERVICE_ROLE_KEY")
    if not base_url or not service_role_key:
        raise RuntimeError("缺少 SUPABASE_URL 或 SUPABASE_SERVICE_ROLE_KEY")
    return SupabaseRestClient(base_url, service_role_key)


def fetch_all_rows(client: SupabaseRestClient, *, limit: Optional[int]) -> List[Dict[str, Any]]:
    rows: List[Dict[str, Any]] = []
    offset = 0
    remaining = limit
    while True:
        page_limit = PAGE_SIZE if remaining is None else min(PAGE_SIZE, remaining)
        batch = client.fetch_rows(TABLE_NAME, "*", limit=page_limit, offset=offset, order="id.asc")
        if not batch:
            break
        rows.extend(batch)
        if len(batch) < page_limit:
            break
        offset += page_limit
        if remaining is not None:
            remaining -= len(batch)
            if remaining <= 0:
                break
    return rows


def normalize_text(value: Any) -> str:
    text = str(value or "").strip().lower()
    text = text.replace("’", "'").replace("“", '"').replace("”", '"')
    text = text.replace("®", "").replace("™", "")
    text = re.sub(r"\s+", " ", text)
    return text


def normalize_entity_name(value: Any) -> str:
    text = normalize_text(value)
    text = text.replace("&", " and ")
    text = re.sub(r"[()/:+,.-]", " ", text)
    text = re.sub(r"\b(full|face|plan|treatment|session|service|services|only|appointment|enhancement|cosmetic|dermal|fillers?)\b", " ", text)
    text = re.sub(r"\s+", " ", text).strip()
    return text


def entity_semantically_matches(entity: str, canonical_name: str, service_name: str) -> bool:
    entity_norm = normalize_entity_name(entity)
    canonical_norm = normalize_entity_name(canonical_name)
    service_norm = normalize_entity_name(service_name)
    if not entity_norm:
        return False
    if entity_norm in {canonical_norm, service_norm}:
        return True
    if canonical_norm and (canonical_norm in entity_norm or entity_norm in canonical_norm):
        return True
    if service_norm and (service_norm in entity_norm or entity_norm in service_norm):
        return True
    return False


def is_multi_entity_bundle_aligned(template_type: str, service_name: str, canonical_name: str) -> bool:
    if template_type != "bundle" or service_name != "Package":
        return False
    return True


def normalize_offer_raw_text(value: Any) -> str:
    text = normalize_text(value)
    text = re.sub(r"https?://\S+", "", text)
    text = re.sub(r"\s+", " ", text).strip(" -;,.")
    return text


def normalize_value(value: Any) -> str:
    if value is None:
        return ""
    if isinstance(value, bool):
        return "true" if value else "false"
    return str(value).strip()


def parse_json_payload(value: Any) -> Tuple[Optional[Any], Optional[str]]:
    if value is None:
        return None, None
    if isinstance(value, (dict, list)):
        return value, None
    raw = str(value).strip()
    if not raw:
        return None, None
    try:
        return json.loads(raw), None
    except json.JSONDecodeError as exc:
        return None, str(exc)


def parse_float(value: Any) -> Optional[float]:
    raw = str(value or "").strip()
    if not raw:
        return None
    raw = raw.replace(",", "")
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


def offer_content_entities(offer_content: Any) -> Tuple[List[str], Optional[str]]:
    payload, error = parse_json_payload(offer_content)
    if error:
        return [], error
    if payload is None:
        return [], None
    if isinstance(payload, dict):
        return [str(key).strip() for key in payload.keys() if str(key).strip()], None
    if isinstance(payload, list):
        entities: List[str] = []
        for item in payload:
            if isinstance(item, dict):
                entities.extend(str(key).strip() for key in item.keys() if str(key).strip())
        return entities, None
    return [], None


def build_exact_duplicate_groups(rows: Sequence[Dict[str, Any]]) -> List[Dict[str, Any]]:
    groups: Dict[Tuple[str, ...], List[Dict[str, Any]]] = defaultdict(list)
    for row in rows:
        fingerprint = tuple(normalize_value(row.get(field)) for field in EXACT_DUPLICATE_FIELDS)
        groups[fingerprint].append(row)

    duplicate_groups: List[Dict[str, Any]] = []
    for fingerprint_rows in groups.values():
        if len(fingerprint_rows) < 2:
            continue
        duplicate_groups.append(
            {
                "count": len(fingerprint_rows),
                "ids": [row.get("id") for row in fingerprint_rows],
                "source_name": fingerprint_rows[0].get("source_name", ""),
                "service_name": fingerprint_rows[0].get("service_name", ""),
                "offer_raw_text": fingerprint_rows[0].get("offer_raw_text", ""),
            }
        )
    duplicate_groups.sort(key=lambda item: (-item["count"], str(item["ids"][0])))
    return duplicate_groups


def build_offer_text_duplicate_groups(rows: Sequence[Dict[str, Any]]) -> List[Dict[str, Any]]:
    groups: Dict[Tuple[str, str], List[Dict[str, Any]]] = defaultdict(list)
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
        service_names = sorted({str(row.get("service_name") or "").strip() for row in group_rows if str(row.get("service_name") or "").strip()})
        duplicates.append(
            {
                "count": len(group_rows),
                "source_name": source_name,
                "normalized_offer_raw_text": normalized_offer,
                "service_names": service_names,
                "ids": [row.get("id") for row in group_rows],
            }
        )
    duplicates.sort(key=lambda item: (-item["count"], item["source_name"], item["normalized_offer_raw_text"]))
    return duplicates


def detect_likely_basis(row: Dict[str, Any]) -> List[str]:
    text = " ".join(
        [
            str(row.get("service_name") or ""),
            str(row.get("offer_raw_text") or ""),
            str(row.get("offer_content") or ""),
            str(row.get("unit_type") or ""),
        ]
    )
    matches: List[str] = []
    for label, pattern in BY_UNIT_PATTERNS:
        if pattern.search(text):
            matches.append(label)
    return matches


def audit_rows(rows: Sequence[Dict[str, Any]]) -> Tuple[List[Issue], List[Dict[str, Any]]]:
    issues: List[Issue] = []
    alignment_rows: List[Dict[str, Any]] = []

    for row in rows:
        promo_offer_id = row.get("id")
        source_name = str(row.get("source_name") or "").strip()
        service_name = str(row.get("service_name") or "").strip()
        service_category = str(row.get("service_category") or "").strip()
        offer_raw_text = str(row.get("offer_raw_text") or "").strip()
        offer_content = row.get("offer_content")
        template_type = normalize_text(row.get("template_type"))
        unit_type = normalize_text(row.get("unit_type"))
        original_price = parse_float(row.get("original_price"))
        discount_price = parse_float(row.get("discount_price"))
        membership_price = parse_float(row.get("membership_price"))
        discount_amount = parse_float(row.get("discount_amount"))
        discount_percent = parse_float(row.get("discount_percent"))
        min_unit = parse_float(row.get("min_unit"))
        delivered_unit = parse_float(row.get("delivered_unit"))
        membership_name = str(row.get("membership_name") or "").strip()
        is_membership_required = parse_bool(row.get("is_membership_required"))

        alignment = infer_alignment(service_name, service_category)
        canonical_name = alignment.get("aligned_service_name_canonical", "")
        alignment_rows.append(
            {
                "id": promo_offer_id,
                "source_name": source_name,
                "service_name": service_name,
                "aligned_service_name_canonical": canonical_name,
                "aligned_service_category": alignment.get("aligned_service_category", ""),
                "needs_manual_review": alignment.get("needs_manual_review", ""),
                "alignment_confidence": alignment.get("alignment_confidence", ""),
                "alignment_note": alignment.get("alignment_note", ""),
            }
        )

        if not source_name:
            issues.append(Issue(promo_offer_id, source_name, service_name, "missing_source_name", "high", "source_name 为空"))
        if not service_name:
            issues.append(Issue(promo_offer_id, source_name, service_name, "missing_service_name", "high", "service_name 为空"))
        if not offer_raw_text:
            issues.append(Issue(promo_offer_id, source_name, service_name, "missing_offer_raw_text", "high", "offer_raw_text 为空"))

        entities, offer_content_error = offer_content_entities(offer_content)
        semantically_aligned = bool(
            canonical_name
            and canonical_name == service_name
            and canonical_name not in {"Package"}
            and entities
            and all(entity_semantically_matches(entity, canonical_name, service_name) for entity in entities)
        )
        bundle_aligned = is_multi_entity_bundle_aligned(template_type, service_name, canonical_name)

        if alignment.get("needs_manual_review") == "TRUE" and not semantically_aligned and not bundle_aligned:
            issues.append(
                Issue(
                    promo_offer_id,
                    source_name,
                    service_name,
                    "service_name_manual_review",
                    "medium",
                    alignment.get("alignment_note", "service_name 需要人工复核"),
                )
            )
        elif canonical_name and canonical_name != service_name:
            issues.append(
                Issue(
                    promo_offer_id,
                    source_name,
                    service_name,
                    "service_name_noncanonical",
                    "medium",
                    f"建议标准化为 {canonical_name}",
                )
            )

        if offer_content_error:
            issues.append(Issue(promo_offer_id, source_name, service_name, "offer_content_malformed_json", "high", offer_content_error))
        elif not entities:
            issues.append(Issue(promo_offer_id, source_name, service_name, "offer_content_empty_or_unstructured", "medium", "offer_content 为空或无法提取实体键"))
        elif canonical_name and canonical_name not in {"", service_name} and not bundle_aligned:
            entity_match = any(entity_semantically_matches(entity, canonical_name, service_name) for entity in entities)
            if not entity_match:
                issues.append(
                    Issue(
                        promo_offer_id,
                        source_name,
                        service_name,
                        "service_offer_content_entity_mismatch",
                        "medium",
                        f"service_name 对齐为 {canonical_name}，但 offer_content 实体为 {entities}",
                    )
                )

        likely_basis = detect_likely_basis(row)
        if "unit" in likely_basis and unit_type != "unit":
            issues.append(Issue(promo_offer_id, source_name, service_name, "unit_type_missing_unit", "high", f"文本像按 unit 报价，但 unit_type={unit_type or '<empty>'}"))
        if "syringe" in likely_basis and unit_type not in {"syringe", "half syringe"}:
            issues.append(Issue(promo_offer_id, source_name, service_name, "unit_type_missing_syringe", "medium", f"文本像按 syringe 报价，但 unit_type={unit_type or '<empty>'}"))
        if unit_type == "unit" and min_unit is None and delivered_unit is None:
            issues.append(Issue(promo_offer_id, source_name, service_name, "unit_type_unit_missing_quantity", "medium", "unit_type=unit，但 min_unit / delivered_unit 都为空"))

        if template_type == "membership" and not membership_name:
            issues.append(Issue(promo_offer_id, source_name, service_name, "membership_missing_name", "medium", "template_type=MEMBERSHIP，但 membership_name 为空"))
        if template_type == "membership" and membership_price is None and not is_membership_required:
            issues.append(Issue(promo_offer_id, source_name, service_name, "membership_missing_price", "medium", "membership 模板没有 membership_price，且未标记 membership required"))
        if template_type == "discount" and discount_price is None and discount_amount is None and discount_percent is None:
            issues.append(Issue(promo_offer_id, source_name, service_name, "discount_missing_discount_fields", "high", "template_type=DISCOUNT，但 discount 相关字段全空"))
        if original_price is not None and discount_price is not None and discount_price > original_price:
            issues.append(Issue(promo_offer_id, source_name, service_name, "discount_price_gt_original_price", "high", f"discount_price={discount_price} 大于 original_price={original_price}"))
        if any(value is not None and value < 0 for value in [original_price, discount_price, membership_price, discount_amount, discount_percent]):
            issues.append(Issue(promo_offer_id, source_name, service_name, "negative_price_field", "high", "存在负数价格/折扣字段"))
        if discount_percent is not None and discount_percent > 100:
            issues.append(Issue(promo_offer_id, source_name, service_name, "discount_percent_gt_100", "high", f"discount_percent={discount_percent}"))
        if membership_price is not None and membership_price == 0:
            issues.append(Issue(promo_offer_id, source_name, service_name, "membership_price_zero", "medium", "membership_price = 0"))

    return issues, alignment_rows


def write_csv(path: Path, rows: Sequence[Dict[str, Any]], fieldnames: Sequence[str]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", newline="", encoding=OUTPUT_ENCODING) as handle:
        writer = csv.DictWriter(handle, fieldnames=list(fieldnames))
        writer.writeheader()
        writer.writerows(rows)


def build_report(
    *,
    row_count: int,
    exact_duplicates: Sequence[Dict[str, Any]],
    text_duplicates: Sequence[Dict[str, Any]],
    issues: Sequence[Issue],
    alignment_rows: Sequence[Dict[str, Any]],
    summary_path: Path,
    issues_path: Path,
    exact_duplicates_path: Path,
    text_duplicates_path: Path,
    alignment_path: Path,
) -> str:
    issue_counter = Counter(issue.issue_type for issue in issues)
    top_issue_lines = "\n".join(f"- `{issue}`: {count}" for issue, count in issue_counter.most_common(15))
    top_noncanonical = Counter(
        row["aligned_service_name_canonical"]
        for row in alignment_rows
        if row["aligned_service_name_canonical"] and row["aligned_service_name_canonical"] != row["service_name"]
    )
    top_alignment_lines = "\n".join(f"- `{name}`: {count}" for name, count in top_noncanonical.most_common(12))
    exact_dup_lines = "\n".join(
        f"- `{item['source_name']}` / `{item['service_name']}`: {item['count']} rows, ids={item['ids'][:8]}"
        for item in list(exact_duplicates)[:10]
    )
    text_dup_lines = "\n".join(
        f"- `{item['source_name']}`: {item['count']} rows share normalized offer_raw_text `{item['normalized_offer_raw_text'][:90]}`"
        for item in list(text_duplicates)[:10]
    )
    return f"""# promo_offer_master Data Quality Audit

- Total rows audited: `{row_count}`
- Exact duplicate groups: `{len(exact_duplicates)}`
- Potential duplicate groups by source_name + normalized offer_raw_text: `{len(text_duplicates)}`
- Total issues flagged: `{len(issues)}`

## Top Issue Types
{top_issue_lines or "- None"}

## Duplicate Findings
### Exact Duplicates
{exact_dup_lines or "- None"}

### Potential Duplicates By offer_raw_text
{text_dup_lines or "- None"}

## Service Name / Offer Content Alignment
- Rows needing manual service_name review: `{sum(1 for row in alignment_rows if row['needs_manual_review'] == 'TRUE')}`
- Rows with non-canonical service_name forms: `{sum(1 for row in alignment_rows if row['aligned_service_name_canonical'] and row['aligned_service_name_canonical'] != row['service_name'])}`

### Top Suggested Canonical Service Entities
{top_alignment_lines or "- None"}

## Output Files
- Summary JSON: `{summary_path}`
- Issues CSV: `{issues_path}`
- Exact duplicates CSV: `{exact_duplicates_path}`
- offer_raw_text duplicate CSV: `{text_duplicates_path}`
- Service alignment CSV: `{alignment_path}`
"""


def main() -> None:
    args = parse_args()
    report_dir = Path(args.report_dir).expanduser().resolve()
    result_dir = Path(args.result_dir).expanduser().resolve()
    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")

    client = load_supabase_client()
    rows = fetch_all_rows(client, limit=args.limit)

    exact_duplicates = build_exact_duplicate_groups(rows)
    text_duplicates = build_offer_text_duplicate_groups(rows)
    issues, alignment_rows = audit_rows(rows)

    issues_rows = [issue.as_row() for issue in issues]
    exact_dup_rows = [
        {
            "count": item["count"],
            "source_name": item["source_name"],
            "service_name": item["service_name"],
            "offer_raw_text": item["offer_raw_text"],
            "ids": json.dumps(item["ids"], ensure_ascii=False),
        }
        for item in exact_duplicates
    ]
    text_dup_rows = [
        {
            "count": item["count"],
            "source_name": item["source_name"],
            "normalized_offer_raw_text": item["normalized_offer_raw_text"],
            "service_names": json.dumps(item["service_names"], ensure_ascii=False),
            "ids": json.dumps(item["ids"], ensure_ascii=False),
        }
        for item in text_duplicates
    ]

    summary = {
        "table": TABLE_NAME,
        "audited_at": datetime.now().isoformat(),
        "row_count": len(rows),
        "exact_duplicate_groups": len(exact_duplicates),
        "exact_duplicate_rows": sum(item["count"] for item in exact_duplicates),
        "offer_raw_text_duplicate_groups": len(text_duplicates),
        "offer_raw_text_duplicate_rows": sum(item["count"] for item in text_duplicates),
        "total_issues": len(issues),
        "issue_type_counts": Counter(issue.issue_type for issue in issues),
        "rows_needing_service_manual_review": sum(1 for row in alignment_rows if row["needs_manual_review"] == "TRUE"),
        "rows_with_noncanonical_service_name": sum(
            1 for row in alignment_rows if row["aligned_service_name_canonical"] and row["aligned_service_name_canonical"] != row["service_name"]
        ),
    }
    summary["issue_type_counts"] = dict(summary["issue_type_counts"])

    summary_path = result_dir / f"promo_offer_master_audit_summary_{timestamp}.json"
    issues_path = result_dir / f"promo_offer_master_audit_issues_{timestamp}.csv"
    exact_duplicates_path = result_dir / f"promo_offer_master_exact_duplicates_{timestamp}.csv"
    text_duplicates_path = result_dir / f"promo_offer_master_offer_text_duplicates_{timestamp}.csv"
    alignment_path = result_dir / f"promo_offer_master_service_alignment_{timestamp}.csv"
    report_path = report_dir / f"promo_offer_master_audit_{timestamp}.md"

    summary_path.parent.mkdir(parents=True, exist_ok=True)
    summary_path.write_text(json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8")
    write_csv(issues_path, issues_rows, ["id", "source_name", "service_name", "issue_type", "severity", "detail"])
    write_csv(exact_duplicates_path, exact_dup_rows, ["count", "source_name", "service_name", "offer_raw_text", "ids"])
    write_csv(text_duplicates_path, text_dup_rows, ["count", "source_name", "normalized_offer_raw_text", "service_names", "ids"])
    write_csv(
        alignment_path,
        alignment_rows,
        [
            "id",
            "source_name",
            "service_name",
            "aligned_service_name_canonical",
            "aligned_service_category",
            "needs_manual_review",
            "alignment_confidence",
            "alignment_note",
        ],
    )

    report = build_report(
        row_count=len(rows),
        exact_duplicates=exact_duplicates,
        text_duplicates=text_duplicates,
        issues=issues,
        alignment_rows=alignment_rows,
        summary_path=summary_path,
        issues_path=issues_path,
        exact_duplicates_path=exact_duplicates_path,
        text_duplicates_path=text_duplicates_path,
        alignment_path=alignment_path,
    )
    report_path.write_text(report, encoding="utf-8")

    print(json.dumps({"summary_path": str(summary_path), "report_path": str(report_path), "row_count": len(rows), "total_issues": len(issues)}, ensure_ascii=False))


if __name__ == "__main__":
    main()
