#!/usr/bin/env python3
"""
Audit promo_website_staging rows that look out of scope for injectable-focused extraction.
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

from config.settings import OUTPUT_DIR

REPORTS_DIR = PROJECT_ROOT / "reports"

SHOP_URL_KEYWORDS = (
    "/shop",
    "/product",
    "/products",
    "/collection",
    "/collections",
    "/store",
)
SHOP_URL_EXCLUSIONS = (
    "shop-services",
    "service-menu",
)
SHOP_CONTENT_PATTERNS = [
    re.compile(r"\badd to cart\b", re.IGNORECASE),
    re.compile(r"\bbuy now\b", re.IGNORECASE),
    re.compile(r"\bcheckout\b", re.IGNORECASE),
    re.compile(r"\bquantity\b", re.IGNORECASE),
    re.compile(r"\bvariant\b", re.IGNORECASE),
    re.compile(r"\bproduct details?\b", re.IGNORECASE),
    re.compile(r"\bshop all\b", re.IGNORECASE),
    re.compile(r"\brebate event\b", re.IGNORECASE),
    re.compile(r"\bbuy online\b", re.IGNORECASE),
]
RETAIL_PRODUCT_PATTERNS = [
    re.compile(r"\bserum\b", re.IGNORECASE),
    re.compile(r"\bcream\b", re.IGNORECASE),
    re.compile(r"\bsunscreen\b", re.IGNORECASE),
    re.compile(r"\bmoisturizer\b", re.IGNORECASE),
    re.compile(r"\bcleanser\b", re.IGNORECASE),
    re.compile(r"\blipstick\b", re.IGNORECASE),
    re.compile(r"\blip gloss\b", re.IGNORECASE),
    re.compile(r"\blip balm\b", re.IGNORECASE),
    re.compile(r"\bliner\b", re.IGNORECASE),
    re.compile(r"\bpowder\b", re.IGNORECASE),
    re.compile(r"\bprimer\b", re.IGNORECASE),
    re.compile(r"\bconcealer\b", re.IGNORECASE),
    re.compile(r"\bfoundation\b", re.IGNORECASE),
    re.compile(r"\bbronzer\b", re.IGNORECASE),
    re.compile(r"\bblush\b", re.IGNORECASE),
    re.compile(r"\bmascara\b", re.IGNORECASE),
    re.compile(r"\bconditioner\b", re.IGNORECASE),
    re.compile(r"\bshampoo\b", re.IGNORECASE),
    re.compile(r"\bcapsules?\b", re.IGNORECASE),
    re.compile(r"\bpads\b", re.IGNORECASE),
    re.compile(r"\bspray\b", re.IGNORECASE),
    re.compile(r"\boil\b", re.IGNORECASE),
]
INJECTABLE_URL_PATTERNS = [
    re.compile(r"/botox", re.IGNORECASE),
    re.compile(r"/dysport", re.IGNORECASE),
    re.compile(r"/xeomin", re.IGNORECASE),
    re.compile(r"/daxxify", re.IGNORECASE),
    re.compile(r"/jeuveau", re.IGNORECASE),
    re.compile(r"/juvederm", re.IGNORECASE),
    re.compile(r"/restylane", re.IGNORECASE),
    re.compile(r"/injectables?", re.IGNORECASE),
    re.compile(r"/fillers?", re.IGNORECASE),
    re.compile(r"/kybella", re.IGNORECASE),
]

HAIR_NAIL_STRONG_PATTERNS = [
    re.compile(r"\bmanicure\b", re.IGNORECASE),
    re.compile(r"\bpedicure\b", re.IGNORECASE),
    re.compile(r"\bacrylic\b", re.IGNORECASE),
    re.compile(r"\bgel[\s-]?x\b", re.IGNORECASE),
    re.compile(r"\bfill-?in\b", re.IGNORECASE),
    re.compile(r"\bnail enhancement\b", re.IGNORECASE),
    re.compile(r"\bbarber\b", re.IGNORECASE),
    re.compile(r"\bblowout\b", re.IGNORECASE),
    re.compile(r"\bbalayage\b", re.IGNORECASE),
    re.compile(r"\bhaircut\b", re.IGNORECASE),
    re.compile(r"\bhair extensions?\b", re.IGNORECASE),
]
HAIR_NAIL_URL_PATTERNS = [
    re.compile(r"/nail", re.IGNORECASE),
    re.compile(r"/hair", re.IGNORECASE),
    re.compile(r"/barber", re.IGNORECASE),
]
HAIR_REMOVAL_PATTERNS = [
    re.compile(r"\blaser hair removal\b", re.IGNORECASE),
    re.compile(r"\bhair removal\b", re.IGNORECASE),
]

NON_INJECTABLE_PATTERNS = [
    re.compile(r"\bfacials?\b", re.IGNORECASE),
    re.compile(r"\bhydrafacial\b", re.IGNORECASE),
    re.compile(r"\bmicroneedling\b", re.IGNORECASE),
    re.compile(r"\bchemical peels?\b", re.IGNORECASE),
    re.compile(r"\bpeels?\b", re.IGNORECASE),
    re.compile(r"\bdermaplan(e|ing)\b", re.IGNORECASE),
    re.compile(r"\blaser hair removal\b", re.IGNORECASE),
    re.compile(r"\blaser\b", re.IGNORECASE),
    re.compile(r"\bbody contouring\b", re.IGNORECASE),
    re.compile(r"\bcoolsculpting\b", re.IGNORECASE),
    re.compile(r"\bemsculpt\b", re.IGNORECASE),
    re.compile(r"\bweight loss\b", re.IGNORECASE),
    re.compile(r"\bsemaglutide\b", re.IGNORECASE),
    re.compile(r"\btirzepatide\b", re.IGNORECASE),
    re.compile(r"\biv therapy\b", re.IGNORECASE),
    re.compile(r"\bmicroneedling\b", re.IGNORECASE),
    re.compile(r"\bspray tans?\b", re.IGNORECASE),
    re.compile(r"\btanning\b", re.IGNORECASE),
    re.compile(r"\bwaxing\b", re.IGNORECASE),
    re.compile(r"\bbrow\b", re.IGNORECASE),
    re.compile(r"\blash\b", re.IGNORECASE),
    re.compile(r"\bpermanent makeup\b", re.IGNORECASE),
    re.compile(r"\bpmu\b", re.IGNORECASE),
    re.compile(r"\bmicroblading\b", re.IGNORECASE),
    re.compile(r"\bombr[eé]\b", re.IGNORECASE),
    re.compile(r"\bpowder brow\b", re.IGNORECASE),
    re.compile(r"\bspa\b", re.IGNORECASE),
    re.compile(r"\bsauna\b", re.IGNORECASE),
    re.compile(r"\bjjimjilbang\b", re.IGNORECASE),
    re.compile(r"\bskin care\b", re.IGNORECASE),
    re.compile(r"\bskincare\b", re.IGNORECASE),
    re.compile(r"\bskin tightening\b", re.IGNORECASE),
    re.compile(r"\bmassage\b", re.IGNORECASE),
    re.compile(r"\bprp\b", re.IGNORECASE),
    re.compile(r"\bprf\b", re.IGNORECASE),
    re.compile(r"\bthread lift\b", re.IGNORECASE),
]

INJECTABLE_PATTERNS = [
    re.compile(r"\bbotox\b", re.IGNORECASE),
    re.compile(r"\bdysport\b", re.IGNORECASE),
    re.compile(r"\bxeomin\b", re.IGNORECASE),
    re.compile(r"\bdaxxify\b", re.IGNORECASE),
    re.compile(r"\bjeuveau\b", re.IGNORECASE),
    re.compile(r"\bneurotoxins?\b", re.IGNORECASE),
    re.compile(r"\blip filler\b", re.IGNORECASE),
    re.compile(r"\bdermal fillers?\b", re.IGNORECASE),
    re.compile(r"\bfillers?\b", re.IGNORECASE),
    re.compile(r"\binjectables?\b", re.IGNORECASE),
    re.compile(r"\bradiesse\b", re.IGNORECASE),
    re.compile(r"\bsculptra\b", re.IGNORECASE),
]

GENERIC_SERVICE_PATTERNS = [
    re.compile(r"\bservices?\b", re.IGNORECASE),
    re.compile(r"\btreatments?\b", re.IGNORECASE),
    re.compile(r"\bsessions?\b", re.IGNORECASE),
    re.compile(r"\bpackage(s)?\b", re.IGNORECASE),
    re.compile(r"\bmembership\b", re.IGNORECASE),
]

SEGMENT_PATTERN = re.compile(r"\[SEGMENT\s+\d+\]\s*", re.IGNORECASE)
WHITESPACE_PATTERN = re.compile(r"\s+")


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
class LabelResult:
    label: str
    confidence_rank: int
    confidence: str
    reason: str
    evidence_snippet: str


def load_supabase_client() -> SupabaseRestClient:
    load_dotenv(PROJECT_ROOT / ".env")
    base_url = os.getenv("SUPABASE_URL")
    service_role_key = os.getenv("SUPABASE_SERVICE_ROLE_KEY")
    if not base_url or not service_role_key:
        raise RuntimeError("缺少 SUPABASE_URL 或 SUPABASE_SERVICE_ROLE_KEY")
    return SupabaseRestClient(base_url, service_role_key)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="审核 promo_website_staging 中偏离 injectable 范围的页面")
    parser.add_argument("--limit", type=int, default=None, help="仅分析前 N 条记录")
    return parser.parse_args()


def fetch_all_rows(client: SupabaseRestClient, *, limit: Optional[int] = None) -> List[Dict[str, Any]]:
    rows: List[Dict[str, Any]] = []
    offset = 0
    page_size = 500
    while True:
        batch = client.fetch_rows(
            "promo_website_staging",
            "promo_website_id,domain_name,subpage_url,name,page_content",
            limit=page_size if limit is None else min(page_size, max(limit - len(rows), 0)),
            offset=offset,
            order="promo_website_id.asc",
        )
        if not batch:
            break
        rows.extend(batch)
        if limit is not None and len(rows) >= limit:
            return rows[:limit]
        if len(batch) < page_size:
            break
        offset += page_size
    return rows


def normalize_text(value: str) -> str:
    text = SEGMENT_PATTERN.sub("", value or "")
    return WHITESPACE_PATTERN.sub(" ", text).strip()


def collect_hits(text: str, patterns: Sequence[re.Pattern[str]]) -> List[str]:
    hits: List[str] = []
    seen = set()
    for pattern in patterns:
        for match in pattern.finditer(text):
            token = match.group(0).strip()
            lowered = token.lower()
            if lowered in seen:
                continue
            seen.add(lowered)
            hits.append(token)
    return hits


def extract_evidence_snippet(text: str, tokens: Sequence[str]) -> str:
    haystack = normalize_text(text)
    lower = haystack.lower()
    for token in tokens:
        index = lower.find(token.lower())
        if index != -1:
            start = max(0, index - 70)
            end = min(len(haystack), index + len(token) + 90)
            snippet = haystack[start:end].strip()
            return snippet[:220]
    return haystack[:220]


def classify_shop_product(url: str, text: str) -> Optional[LabelResult]:
    url_lower = (url or "").lower()
    if any(exclusion in url_lower for exclusion in SHOP_URL_EXCLUSIONS):
        return None

    url_hit = any(keyword in url_lower for keyword in SHOP_URL_KEYWORDS)
    retail_hits = collect_hits(text, SHOP_CONTENT_PATTERNS)
    retail_product_hits = collect_hits(text, RETAIL_PRODUCT_PATTERNS)
    injectable_hits = collect_hits(text, INJECTABLE_PATTERNS)
    non_injectable_hits = collect_hits(text, NON_INJECTABLE_PATTERNS)
    generic_service_hits = collect_hits(text, GENERIC_SERVICE_PATTERNS)

    strong_retail_only = bool(retail_hits) and (bool(retail_product_hits) or len(injectable_hits) + len(non_injectable_hits) <= 1)
    product_listing = "ulta.com" in url_lower or "/product/" in url_lower or "/products/" in url_lower
    service_dominant = len(injectable_hits) + len(non_injectable_hits) + len(generic_service_hits) > len(retail_hits) + 1

    if not url_hit and not retail_hits:
        return None
    if not retail_product_hits and not product_listing and "ulta.com" not in url_lower:
        return None
    if not url_hit and not (
        len(retail_hits) >= 1
        and len(retail_product_hits) >= 2
        and not injectable_hits
        and len(generic_service_hits) == 0
    ):
        return None
    if injectable_hits and not retail_product_hits and "ulta.com" not in url_lower:
        return None
    if service_dominant and not strong_retail_only and not product_listing:
        return None
    if not retail_hits and not product_listing and not retail_product_hits:
        return None

    confidence = "high" if url_hit and (retail_hits or retail_product_hits) else "medium" if retail_hits else "low"
    reason_bits = []
    if url_hit:
        reason_bits.append("URL matches shop/product/collection path")
    if retail_hits:
        reason_bits.append(f"content includes retail cues: {', '.join(retail_hits[:3])}")
    if retail_product_hits:
        reason_bits.append(f"content includes retail product terms: {', '.join(retail_product_hits[:3])}")
    evidence_tokens = retail_hits or retail_product_hits or ["/shop"]
    return LabelResult(
        label="shop_product",
        confidence_rank={"low": 1, "medium": 2, "high": 3}[confidence],
        confidence=confidence,
        reason="; ".join(reason_bits),
        evidence_snippet=extract_evidence_snippet(text if retail_hits else url, evidence_tokens),
    )


def classify_hair_nail(url: str, text: str) -> Optional[LabelResult]:
    url_hits = collect_hits(url, HAIR_NAIL_URL_PATTERNS)
    text_hits = collect_hits(text, HAIR_NAIL_STRONG_PATTERNS)
    hair_removal_hits = collect_hits(text, HAIR_REMOVAL_PATTERNS)

    if not url_hits and not text_hits:
        return None
    if not text_hits and hair_removal_hits:
        return None

    confidence = "high" if url_hits and len(text_hits) >= 1 else "medium" if len(text_hits) >= 2 else "low"
    reason_bits = []
    if url_hits:
        reason_bits.append(f"URL points to hair/nail/salon path: {', '.join(url_hits[:2])}")
    if text_hits:
        reason_bits.append(f"content is dominated by hair/nail service terms: {', '.join(text_hits[:3])}")
    return LabelResult(
        label="hair_nail",
        confidence_rank={"low": 1, "medium": 2, "high": 3}[confidence],
        confidence=confidence,
        reason="; ".join(reason_bits),
        evidence_snippet=extract_evidence_snippet(text, text_hits or url_hits),
    )


def classify_other_non_injectable(url: str, text: str, hair_nail_hit: bool) -> Optional[LabelResult]:
    non_injectable_hits = collect_hits(text, NON_INJECTABLE_PATTERNS)
    injectable_hits = collect_hits(text, INJECTABLE_PATTERNS)
    generic_service_hits = collect_hits(text, GENERIC_SERVICE_PATTERNS)
    retail_product_hits = collect_hits(text, RETAIL_PRODUCT_PATTERNS)
    url_lower = (url or "").lower()
    injectable_url_hits = collect_hits(url, INJECTABLE_URL_PATTERNS)

    url_signal = any(
        keyword in url_lower
        for keyword in (
            "facial",
            "laser",
            "weight-loss",
            "weight_loss",
            "coolsculpt",
            "peel",
            "microneed",
            "spa",
            "brow",
            "lash",
            "wax",
            "prp",
            "prf",
            "thread-lift",
            "hair-removal",
        )
    )

    if hair_nail_hit and not non_injectable_hits:
        non_injectable_hits = ["hair/nail service"]

    if not non_injectable_hits and not url_signal:
        return None

    if injectable_url_hits:
        return None
    if any(keyword in url_lower for keyword in SHOP_URL_KEYWORDS) and retail_product_hits and len(non_injectable_hits) <= 1:
        return None

    noninj_score = len(non_injectable_hits) + (1 if url_signal else 0) + len(generic_service_hits) // 2
    inj_score = len(injectable_hits)
    if inj_score > 0 and noninj_score <= inj_score:
        return None

    confidence = "high" if url_signal and len(non_injectable_hits) >= 2 else "medium" if len(non_injectable_hits) >= 2 else "low"
    reason_bits = []
    if url_signal:
        reason_bits.append("URL points to a non-injectable service area")
    if non_injectable_hits:
        reason_bits.append(f"content is mainly non-injectable services: {', '.join(non_injectable_hits[:4])}")
    if injectable_hits:
        reason_bits.append(f"injectable terms exist but are not dominant: {', '.join(injectable_hits[:2])}")
    return LabelResult(
        label="other_non_injectable",
        confidence_rank={"low": 1, "medium": 2, "high": 3}[confidence],
        confidence=confidence,
        reason="; ".join(reason_bits),
        evidence_snippet=extract_evidence_snippet(text, non_injectable_hits or ["service"]),
    )


def classify_row(row: Dict[str, Any]) -> List[LabelResult]:
    url = (row.get("subpage_url") or "").strip()
    text = normalize_text(row.get("page_content") or "")
    if not url or not text:
        return []

    results: List[LabelResult] = []

    shop_hit = classify_shop_product(url, text)
    if shop_hit:
        results.append(shop_hit)

    hair_nail_hit = classify_hair_nail(url, text)
    if hair_nail_hit:
        results.append(hair_nail_hit)

    other_hit = classify_other_non_injectable(url, text, hair_nail_hit=hair_nail_hit is not None)
    if other_hit:
        results.append(other_hit)

    return results


def build_detailed_rows(rows: Iterable[Dict[str, Any]]) -> List[Dict[str, Any]]:
    detailed_rows: List[Dict[str, Any]] = []
    for row in rows:
        label_results = classify_row(row)
        if not label_results:
            continue
        label_results.sort(key=lambda item: (-item.confidence_rank, item.label))
        detailed_rows.append(
            {
                "promo_website_id": row.get("promo_website_id"),
                "domain_name": row.get("domain_name", ""),
                "subpage_url": row.get("subpage_url", ""),
                "name": row.get("name", "") or "",
                "matched_labels": ", ".join(item.label for item in label_results),
                "reason": " | ".join(f"{item.label}: {item.reason}" for item in label_results),
                "evidence_snippet": " | ".join(f"{item.label}: {item.evidence_snippet}" for item in label_results),
                "confidence": label_results[0].confidence,
            }
        )
    detailed_rows.sort(key=lambda item: (item["domain_name"], item["subpage_url"]))
    return detailed_rows


def write_csv(path: Path, rows: Sequence[Dict[str, Any]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fieldnames = [
        "promo_website_id",
        "domain_name",
        "subpage_url",
        "name",
        "matched_labels",
        "reason",
        "evidence_snippet",
        "confidence",
    ]
    with path.open("w", newline="", encoding="utf-8-sig") as handle:
        writer = csv.DictWriter(handle, fieldnames=fieldnames)
        writer.writeheader()
        writer.writerows(rows)


def build_summary_rows(detailed_rows: Sequence[Dict[str, Any]]) -> List[Dict[str, Any]]:
    label_counter: Counter[str] = Counter()
    label_domains: Dict[str, set[str]] = defaultdict(set)
    label_samples: Dict[str, List[str]] = defaultdict(list)

    for row in detailed_rows:
        labels = [label.strip() for label in row["matched_labels"].split(",") if label.strip()]
        for label in labels:
            label_counter[label] += 1
            label_domains[label].add(row["domain_name"])
            if len(label_samples[label]) < 3:
                label_samples[label].append(row["subpage_url"])

    summary_rows: List[Dict[str, Any]] = []
    for label in ("shop_product", "hair_nail", "other_non_injectable"):
        summary_rows.append(
            {
                "label": label,
                "matched_rows": label_counter[label],
                "matched_domains": len(label_domains[label]),
                "sample_urls": " | ".join(label_samples[label]),
            }
        )
    return summary_rows


def render_markdown_table(rows: Sequence[Dict[str, Any]], columns: Sequence[Tuple[str, str]]) -> str:
    header = "| " + " | ".join(column_title for _, column_title in columns) + " |"
    separator = "| " + " | ".join("---" for _ in columns) + " |"
    body = []
    for row in rows:
        body.append("| " + " | ".join(str(row.get(column_key, "")).replace("\n", " ") for column_key, _ in columns) + " |")
    return "\n".join([header, separator, *body]) if body else "\n".join([header, separator])


def write_markdown_report(
    path: Path,
    *,
    total_rows: int,
    detailed_rows: Sequence[Dict[str, Any]],
    summary_rows: Sequence[Dict[str, Any]],
) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    grouped_rows: Dict[str, List[Dict[str, Any]]] = defaultdict(list)
    for row in detailed_rows:
        for label in [label.strip() for label in row["matched_labels"].split(",") if label.strip()]:
            grouped_rows[label].append(row)

    summary_table = render_markdown_table(
        summary_rows,
        [
            ("label", "label"),
            ("matched_rows", "matched_rows"),
            ("matched_domains", "matched_domains"),
            ("sample_urls", "sample_urls"),
        ],
    )

    sections = [
        "# promo_website_staging_scope_audit",
        "",
        f"- analyzed_rows: {total_rows}",
        f"- flagged_rows: {len(detailed_rows)}",
        "",
        "## summary",
        "",
        summary_table,
    ]

    detail_columns = [
        ("promo_website_id", "promo_website_id"),
        ("domain_name", "domain_name"),
        ("subpage_url", "subpage_url"),
        ("matched_labels", "matched_labels"),
        ("confidence", "confidence"),
        ("reason", "reason"),
        ("evidence_snippet", "evidence_snippet"),
    ]
    for label in ("shop_product", "hair_nail", "other_non_injectable"):
        rows = grouped_rows.get(label, [])
        rows = sorted(rows, key=lambda item: (item["domain_name"], item["subpage_url"]))
        sections.extend(
            [
                "",
                f"## {label}",
                "",
                render_markdown_table(rows, detail_columns),
            ]
        )

    path.write_text("\n".join(sections) + "\n", encoding="utf-8")


def write_json_report(path: Path, payload: Dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")


def main() -> None:
    args = parse_args()
    client = load_supabase_client()
    rows = fetch_all_rows(client, limit=args.limit)
    detailed_rows = build_detailed_rows(rows)
    summary_rows = build_summary_rows(detailed_rows)

    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    csv_path = OUTPUT_DIR / f"promo_website_staging_scope_audit_details_{timestamp}.csv"
    md_path = REPORTS_DIR / f"promo_website_staging_scope_audit_{timestamp}.md"
    json_path = OUTPUT_DIR / f"promo_website_staging_scope_audit_summary_{timestamp}.json"

    write_csv(csv_path, detailed_rows)
    write_markdown_report(md_path, total_rows=len(rows), detailed_rows=detailed_rows, summary_rows=summary_rows)
    write_json_report(
        json_path,
        {
            "analyzed_rows": len(rows),
            "flagged_rows": len(detailed_rows),
            "summary": summary_rows,
            "csv_path": str(csv_path),
            "markdown_report_path": str(md_path),
        },
    )

    print(
        json.dumps(
            {
                "analyzed_rows": len(rows),
                "flagged_rows": len(detailed_rows),
                "summary": summary_rows,
                "csv_path": str(csv_path),
                "markdown_report_path": str(md_path),
                "json_summary_path": str(json_path),
            },
            ensure_ascii=False,
            indent=2,
        )
    )


if __name__ == "__main__":
    main()
