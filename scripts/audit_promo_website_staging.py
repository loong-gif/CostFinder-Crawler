#!/usr/bin/env python3
"""
Audit promo_website_staging data quality and its coverage as a source for promo_offer_master.
"""
from __future__ import annotations

import argparse
import csv
import hashlib
import json
import os
import re
import sys
from collections import Counter, defaultdict
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Sequence, Tuple
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

import requests
from dotenv import load_dotenv

PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from config.settings import OUTPUT_ENCODING  # noqa: E402


DEFAULT_REPORT_DIR = PROJECT_ROOT / "reports"
DEFAULT_RESULT_DIR = PROJECT_ROOT / "output" / "results"
PAGE_SIZE = 1000

TRACKING_QUERY_KEYS = {
    "utm_source",
    "utm_medium",
    "utm_campaign",
    "utm_term",
    "utm_content",
    "fbclid",
    "gclid",
    "gbraid",
    "wbraid",
}

PRICE_SIGNAL_PATTERNS = [
    re.compile(r"\$\s*\d+(?:,\d{3})*(?:\.\d{1,2})?", re.IGNORECASE),
    re.compile(r"\b\d+(?:\.\d+)?\s*%\s*(?:off|discount|savings?)\b", re.IGNORECASE),
    re.compile(r"\b(?:price|pricing|starts? at|from|per unit|per syringe|membership|specials?|offers?|promo|deal|discount)\b", re.IGNORECASE),
]

BOILERPLATE_PATTERNS = [
    re.compile(r"\bprivacy policy\b", re.IGNORECASE),
    re.compile(r"\bterms (?:of use|and conditions|of service)\b", re.IGNORECASE),
    re.compile(r"\bcookie policy\b", re.IGNORECASE),
    re.compile(r"\ball rights reserved\b", re.IGNORECASE),
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
            }
        )

    def fetch_rows(
        self,
        table: str,
        select: str,
        *,
        limit: Optional[int] = None,
        offset: Optional[int] = None,
        order: Optional[str] = None,
    ) -> List[Dict[str, Any]]:
        params: Dict[str, str] = {"select": select}
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
    table_name: str
    row_id: Any
    domain_name: str
    subpage_url: str
    issue_type: str
    severity: str
    detail: str

    def as_row(self) -> Dict[str, Any]:
        return {
            "table_name": self.table_name,
            "row_id": self.row_id,
            "domain_name": self.domain_name,
            "subpage_url": self.subpage_url,
            "issue_type": self.issue_type,
            "severity": self.severity,
            "detail": self.detail,
        }


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Audit promo_website_staging data quality")
    parser.add_argument("--limit", type=int, default=None, help="Only audit the first N staging rows")
    parser.add_argument("--report-dir", default=str(DEFAULT_REPORT_DIR), help="Markdown report output directory")
    parser.add_argument("--result-dir", default=str(DEFAULT_RESULT_DIR), help="Structured result output directory")
    return parser.parse_args()


def load_client() -> SupabaseRestClient:
    load_dotenv(PROJECT_ROOT / ".env")
    base_url = os.getenv("SUPABASE_URL")
    service_role_key = os.getenv("SUPABASE_SERVICE_ROLE_KEY")
    if not base_url or not service_role_key:
        raise RuntimeError("Missing SUPABASE_URL or SUPABASE_SERVICE_ROLE_KEY")
    return SupabaseRestClient(base_url, service_role_key)


def fetch_all_rows(
    client: SupabaseRestClient,
    table: str,
    select: str,
    *,
    limit: Optional[int],
    order: str,
) -> List[Dict[str, Any]]:
    rows: List[Dict[str, Any]] = []
    offset = 0
    remaining = limit
    while True:
        page_limit = PAGE_SIZE if remaining is None else min(PAGE_SIZE, remaining)
        batch = client.fetch_rows(table, select, limit=page_limit, offset=offset, order=order)
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


def normalize_domain(value: Any) -> str:
    text = str(value or "").strip().lower()
    text = re.sub(r"^https?://", "", text)
    text = text.split("/")[0].split("?")[0].split("#")[0]
    text = text[4:] if text.startswith("www.") else text
    return text.strip(".")


def parse_url(value: Any) -> Optional[Any]:
    raw = str(value or "").strip()
    if not raw:
        return None
    parsed = urlsplit(raw)
    if not parsed.scheme or not parsed.netloc:
        return None
    if parsed.scheme.lower() not in {"http", "https"}:
        return None
    return parsed


def normalize_url(value: Any) -> str:
    parsed = parse_url(value)
    if not parsed:
        return ""
    scheme = "https"
    netloc = normalize_domain(parsed.netloc)
    path = re.sub(r"/+", "/", parsed.path or "/")
    if path != "/":
        path = path.rstrip("/")
    query_items = [(k, v) for k, v in parse_qsl(parsed.query, keep_blank_values=True) if k.lower() not in TRACKING_QUERY_KEYS]
    query = urlencode(query_items, doseq=True)
    return urlunsplit((scheme, netloc, path, query, ""))


def domains_match(url_domain: str, row_domain: str) -> bool:
    if not url_domain or not row_domain:
        return False
    return url_domain == row_domain or url_domain.endswith("." + row_domain) or row_domain.endswith("." + url_domain)


def normalize_content(value: Any) -> str:
    text = str(value or "")
    text = re.sub(r"\[SEGMENT\s+\d+\]\s*", " ", text, flags=re.IGNORECASE)
    text = re.sub(r"\s+", " ", text).strip().lower()
    return text


def content_hash(value: Any) -> str:
    normalized = normalize_content(value)
    if not normalized:
        return ""
    return hashlib.sha256(normalized.encode("utf-8")).hexdigest()


def has_price_signal(text: str) -> bool:
    return any(pattern.search(text or "") for pattern in PRICE_SIGNAL_PATTERNS)


def is_mostly_boilerplate(text: str) -> bool:
    normalized = normalize_content(text)
    if not normalized:
        return False
    hits = sum(1 for pattern in BOILERPLATE_PATTERNS if pattern.search(normalized))
    return hits >= 2 and not has_price_signal(normalized)


def parse_timestamp(value: Any) -> Optional[datetime]:
    raw = str(value or "").strip()
    if not raw:
        return None
    raw = re.sub(r"(\.\d{1,5})([+-]\d{2}:\d{2})$", lambda m: m.group(1).ljust(7, "0") + m.group(2), raw)
    try:
        return datetime.fromisoformat(raw.replace("Z", "+00:00"))
    except ValueError:
        return None


def status_value(value: Any) -> str:
    if value is None:
        return "<null>"
    return str(value).strip().lower()


def add_issue(issues: List[Issue], row: Dict[str, Any], issue_type: str, severity: str, detail: str) -> None:
    issues.append(
        Issue(
            table_name="promo_website_staging",
            row_id=row.get("promo_website_id"),
            domain_name=str(row.get("domain_name") or ""),
            subpage_url=str(row.get("subpage_url") or ""),
            issue_type=issue_type,
            severity=severity,
            detail=detail,
        )
    )


def analyze_staging_rows(rows: Sequence[Dict[str, Any]]) -> Tuple[List[Issue], Dict[str, Any], List[Dict[str, Any]], List[Dict[str, Any]]]:
    issues: List[Issue] = []
    normalized_url_groups: Dict[str, List[Dict[str, Any]]] = defaultdict(list)
    content_hash_groups: Dict[str, List[Dict[str, Any]]] = defaultdict(list)
    domain_counter: Counter[str] = Counter()
    status_counter: Counter[str] = Counter()
    content_lengths: List[int] = []
    now = datetime.now(timezone.utc)

    for row in rows:
        row_id = row.get("promo_website_id")
        domain = normalize_domain(row.get("domain_name"))
        subpage_url = str(row.get("subpage_url") or "").strip()
        page_content = str(row.get("page_content") or "")
        normalized_page_content = normalize_content(page_content)
        parsed_url = parse_url(subpage_url)
        normalized_url = normalize_url(subpage_url)
        content_len = len(page_content)

        if domain:
            domain_counter[domain] += 1
        status_counter[status_value(row.get("processed_status"))] += 1
        content_lengths.append(content_len)

        if normalized_url:
            normalized_url_groups[normalized_url].append(row)
        page_hash = content_hash(page_content)
        if page_hash and content_len >= 250:
            content_hash_groups[page_hash].append(row)

        if row_id in (None, ""):
            add_issue(issues, row, "missing_primary_key", "high", "promo_website_id is empty")
        if not subpage_url:
            add_issue(issues, row, "missing_subpage_url", "high", "subpage_url is empty")
        elif not parsed_url:
            add_issue(issues, row, "invalid_subpage_url", "high", f"subpage_url is not a valid http(s) URL: {subpage_url}")
        elif domain and not domains_match(normalize_domain(parsed_url.netloc), domain):
            add_issue(
                issues,
                row,
                "domain_url_mismatch",
                "medium",
                f"domain_name={domain} does not match URL host={normalize_domain(parsed_url.netloc)}",
            )

        if not domain:
            add_issue(issues, row, "missing_domain_name", "medium", "domain_name is empty")
        if not str(row.get("name") or "").strip():
            add_issue(issues, row, "missing_name", "low", "name is empty, which weakens source_name mapping")

        timestamp = parse_timestamp(row.get("crawl_timestamp"))
        if not row.get("crawl_timestamp"):
            add_issue(issues, row, "missing_crawl_timestamp", "medium", "crawl_timestamp is empty")
        elif not timestamp:
            add_issue(issues, row, "invalid_crawl_timestamp", "medium", f"crawl_timestamp is not ISO parseable: {row.get('crawl_timestamp')}")
        elif timestamp > now:
            add_issue(issues, row, "future_crawl_timestamp", "medium", f"crawl_timestamp={timestamp.isoformat()} is in the future")

        status = status_value(row.get("processed_status"))
        if status == "<null>" or status == "":
            add_issue(issues, row, "missing_processed_status", "low", "processed_status is empty")
        elif status not in {"true", "false"}:
            add_issue(issues, row, "unexpected_processed_status", "medium", f"processed_status={row.get('processed_status')}")

        if not page_content.strip():
            add_issue(issues, row, "missing_page_content", "high", "page_content is empty")
        elif content_len < 250:
            add_issue(issues, row, "page_content_too_short", "medium", f"page_content length={content_len}")
        elif content_len > 120000:
            add_issue(issues, row, "page_content_very_long", "low", f"page_content length={content_len}")

        if page_content.strip() and not has_price_signal(normalized_page_content):
            add_issue(issues, row, "content_without_price_signal", "low", "page_content has no obvious price, discount, membership, offer, or pricing signal")
        if page_content.strip() and is_mostly_boilerplate(page_content):
            add_issue(issues, row, "content_mostly_boilerplate", "medium", "page_content appears to be legal/footer boilerplate")

    duplicate_url_rows: List[Dict[str, Any]] = []
    for normalized_url, group in normalized_url_groups.items():
        if len(group) < 2:
            continue
        ids = [item.get("promo_website_id") for item in group]
        domains = sorted({normalize_domain(item.get("domain_name")) for item in group if normalize_domain(item.get("domain_name"))})
        duplicate_url_rows.append({"normalized_subpage_url": normalized_url, "count": len(group), "ids": json.dumps(ids), "domains": json.dumps(domains)})
        for item in group:
            add_issue(issues, item, "duplicate_normalized_subpage_url", "high", f"normalized_subpage_url appears {len(group)} times; ids={ids}")

    duplicate_content_rows: List[Dict[str, Any]] = []
    for hash_value, group in content_hash_groups.items():
        if len(group) < 2:
            continue
        ids = [item.get("promo_website_id") for item in group]
        urls = [item.get("subpage_url") for item in group[:10]]
        domains = sorted({normalize_domain(item.get("domain_name")) for item in group if normalize_domain(item.get("domain_name"))})
        duplicate_content_rows.append(
            {
                "content_hash": hash_value,
                "count": len(group),
                "ids": json.dumps(ids),
                "domains": json.dumps(domains),
                "sample_urls": json.dumps(urls, ensure_ascii=False),
            }
        )
        severity = "medium" if len(domains) > 1 else "low"
        for item in group:
            add_issue(issues, item, "duplicate_page_content", severity, f"identical normalized page_content appears {len(group)} times; ids={ids}")

    content_lengths_sorted = sorted(content_lengths)
    summary = {
        "row_count": len(rows),
        "unique_domains": len(domain_counter),
        "processed_status_counts": dict(status_counter),
        "top_domains_by_row_count": domain_counter.most_common(20),
        "content_length_min": content_lengths_sorted[0] if content_lengths_sorted else 0,
        "content_length_median": content_lengths_sorted[len(content_lengths_sorted) // 2] if content_lengths_sorted else 0,
        "content_length_max": content_lengths_sorted[-1] if content_lengths_sorted else 0,
        "duplicate_normalized_url_groups": len(duplicate_url_rows),
        "duplicate_normalized_url_rows": sum(row["count"] for row in duplicate_url_rows),
        "duplicate_content_groups": len(duplicate_content_rows),
        "duplicate_content_rows": sum(row["count"] for row in duplicate_content_rows),
    }
    return issues, summary, duplicate_url_rows, duplicate_content_rows


def analyze_offer_master_coverage(
    staging_rows: Sequence[Dict[str, Any]],
    offer_rows: Sequence[Dict[str, Any]],
) -> Tuple[List[Issue], Dict[str, Any], List[Dict[str, Any]], List[Dict[str, Any]]]:
    issues: List[Issue] = []
    staging_by_url: Dict[str, List[Dict[str, Any]]] = defaultdict(list)
    for row in staging_rows:
        normalized_url = normalize_url(row.get("subpage_url"))
        if normalized_url:
            staging_by_url[normalized_url].append(row)

    offer_by_url: Dict[str, List[Dict[str, Any]]] = defaultdict(list)
    website_offer_rows = [row for row in offer_rows if str(row.get("channel") or "").strip().lower() == "website"]
    missing_source_url_count = 0
    for row in website_offer_rows:
        normalized_url = normalize_url(row.get("source_url"))
        if normalized_url:
            offer_by_url[normalized_url].append(row)
        else:
            missing_source_url_count += 1
            issues.append(
                Issue(
                    table_name="promo_offer_master",
                    row_id=row.get("id"),
                    domain_name="",
                    subpage_url=str(row.get("source_url") or ""),
                    issue_type="offer_missing_or_invalid_source_url",
                    severity="high",
                    detail="Website offer row has missing or invalid source_url",
                )
            )

    staging_without_offers: List[Dict[str, Any]] = []
    for normalized_url, group in staging_by_url.items():
        if normalized_url in offer_by_url:
            continue
        for row in group:
            staging_without_offers.append(
                {
                    "promo_website_id": row.get("promo_website_id"),
                    "domain_name": row.get("domain_name"),
                    "name": row.get("name"),
                    "subpage_url": row.get("subpage_url"),
                    "processed_status": row.get("processed_status"),
                    "page_content_length": len(str(row.get("page_content") or "")),
                }
            )

    offers_without_staging: List[Dict[str, Any]] = []
    for normalized_url, group in offer_by_url.items():
        if normalized_url in staging_by_url:
            continue
        for row in group:
            offers_without_staging.append(
                {
                    "promo_offer_master_id": row.get("id"),
                    "source_name": row.get("source_name"),
                    "source_url": row.get("source_url"),
                    "template_type": row.get("template_type"),
                    "service_name": row.get("service_name"),
                }
            )
            issues.append(
                Issue(
                    table_name="promo_offer_master",
                    row_id=row.get("id"),
                    domain_name="",
                    subpage_url=str(row.get("source_url") or ""),
                    issue_type="offer_source_url_not_in_staging",
                    severity="medium",
                    detail="Website offer source_url does not match any promo_website_staging.subpage_url after URL normalization",
                )
            )

    processed_true_without_offers = sum(
        1 for row in staging_without_offers if status_value(row.get("processed_status")) == "true"
    )
    coverage_summary = {
        "website_offer_rows": len(website_offer_rows),
        "website_offer_rows_missing_or_invalid_source_url": missing_source_url_count,
        "unique_staging_urls": len(staging_by_url),
        "unique_offer_source_urls": len(offer_by_url),
        "staging_rows_without_offer_master_rows": len(staging_without_offers),
        "processed_true_staging_rows_without_offer_master_rows": processed_true_without_offers,
        "offer_master_rows_without_staging_source_url": len(offers_without_staging),
    }
    return issues, coverage_summary, staging_without_offers, offers_without_staging


def write_csv(path: Path, rows: Sequence[Dict[str, Any]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fieldnames: List[str] = []
    seen = set()
    for row in rows:
        for key in row.keys():
            if key not in seen:
                fieldnames.append(key)
                seen.add(key)
    if not fieldnames:
        fieldnames = ["empty"]
        rows = [{"empty": ""}]
    with path.open("w", newline="", encoding=OUTPUT_ENCODING) as handle:
        writer = csv.DictWriter(handle, fieldnames=fieldnames)
        writer.writeheader()
        writer.writerows(rows)


def build_report(
    *,
    summary: Dict[str, Any],
    staging_summary: Dict[str, Any],
    coverage_summary: Dict[str, Any],
    paths: Dict[str, Path],
) -> str:
    issue_lines = "\n".join(f"- `{issue}`: {count}" for issue, count in summary["issue_type_counts"].items()) or "- None"
    severity_lines = "\n".join(f"- `{severity}`: {count}" for severity, count in summary["severity_counts"].items()) or "- None"
    top_domains = "\n".join(f"- `{domain}`: {count}" for domain, count in staging_summary["top_domains_by_row_count"][:15]) or "- None"
    return f"""# promo_website_staging Data Quality Audit

- Total staging rows audited: `{staging_summary['row_count']}`
- Unique staging domains: `{staging_summary['unique_domains']}`
- Total issues flagged: `{summary['total_issues']}`
- Exact URL duplicate groups after normalization: `{staging_summary['duplicate_normalized_url_groups']}`
- Duplicate content groups: `{staging_summary['duplicate_content_groups']}`

## Issue Counts
{issue_lines}

## Severity Counts
{severity_lines}

## Processed Status
`{json.dumps(staging_summary['processed_status_counts'], ensure_ascii=False)}`

## Content Length
- Min: `{staging_summary['content_length_min']}`
- Median: `{staging_summary['content_length_median']}`
- Max: `{staging_summary['content_length_max']}`

## Source Coverage Into promo_offer_master
- Website offer rows: `{coverage_summary['website_offer_rows']}`
- Unique staging URLs: `{coverage_summary['unique_staging_urls']}`
- Unique offer source URLs: `{coverage_summary['unique_offer_source_urls']}`
- Staging rows without matching offer rows: `{coverage_summary['staging_rows_without_offer_master_rows']}`
- Processed staging rows without matching offer rows: `{coverage_summary['processed_true_staging_rows_without_offer_master_rows']}`
- Offer rows whose source_url is missing from staging: `{coverage_summary['offer_master_rows_without_staging_source_url']}`
- Website offer rows with missing/invalid source_url: `{coverage_summary['website_offer_rows_missing_or_invalid_source_url']}`

## Top Domains By Staging Row Count
{top_domains}

## Output Files
- Summary JSON: `{paths['summary']}`
- Issue CSV: `{paths['issues']}`
- Duplicate URL CSV: `{paths['duplicate_urls']}`
- Duplicate Content CSV: `{paths['duplicate_content']}`
- Staging Without Offers CSV: `{paths['staging_without_offers']}`
- Offers Without Staging CSV: `{paths['offers_without_staging']}`
"""


def main() -> None:
    args = parse_args()
    report_dir = Path(args.report_dir).expanduser().resolve()
    result_dir = Path(args.result_dir).expanduser().resolve()
    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")

    client = load_client()
    staging_rows = fetch_all_rows(
        client,
        "promo_website_staging",
        "*",
        limit=args.limit,
        order="promo_website_id.asc",
    )
    offer_rows = fetch_all_rows(
        client,
        "promo_offer_master",
        "id,channel,source_url,source_name,template_type,service_name",
        limit=None,
        order="id.asc",
    )

    staging_issues, staging_summary, duplicate_urls, duplicate_content = analyze_staging_rows(staging_rows)
    coverage_issues, coverage_summary, staging_without_offers, offers_without_staging = analyze_offer_master_coverage(staging_rows, offer_rows)
    issues = staging_issues + coverage_issues
    issue_rows = [issue.as_row() for issue in issues]

    issue_counter = Counter(issue.issue_type for issue in issues)
    severity_counter = Counter(issue.severity for issue in issues)
    summary = {
        "table": "promo_website_staging",
        "audited_at": datetime.now(timezone.utc).isoformat(),
        "staging_summary": staging_summary,
        "promo_offer_master_coverage": coverage_summary,
        "total_issues": len(issues),
        "issue_type_counts": dict(issue_counter.most_common()),
        "severity_counts": dict(severity_counter.most_common()),
    }

    paths = {
        "summary": result_dir / f"promo_website_staging_audit_summary_{timestamp}.json",
        "issues": result_dir / f"promo_website_staging_audit_issues_{timestamp}.csv",
        "duplicate_urls": result_dir / f"promo_website_staging_duplicate_urls_{timestamp}.csv",
        "duplicate_content": result_dir / f"promo_website_staging_duplicate_content_{timestamp}.csv",
        "staging_without_offers": result_dir / f"promo_website_staging_without_offer_master_{timestamp}.csv",
        "offers_without_staging": result_dir / f"promo_offer_master_without_website_staging_{timestamp}.csv",
        "report": report_dir / f"promo_website_staging_audit_{timestamp}.md",
    }

    paths["summary"].parent.mkdir(parents=True, exist_ok=True)
    paths["report"].parent.mkdir(parents=True, exist_ok=True)
    paths["summary"].write_text(json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8")
    write_csv(paths["issues"], issue_rows)
    write_csv(paths["duplicate_urls"], duplicate_urls)
    write_csv(paths["duplicate_content"], duplicate_content)
    write_csv(paths["staging_without_offers"], staging_without_offers)
    write_csv(paths["offers_without_staging"], offers_without_staging)

    report = build_report(summary=summary, staging_summary=staging_summary, coverage_summary=coverage_summary, paths=paths)
    paths["report"].write_text(report, encoding="utf-8")

    print(
        json.dumps(
            {
                "summary_path": str(paths["summary"]),
                "report_path": str(paths["report"]),
                "row_count": staging_summary["row_count"],
                "total_issues": len(issues),
            },
            ensure_ascii=False,
        )
    )


if __name__ == "__main__":
    main()
