#!/usr/bin/env python3
"""Audit promo_offer_master offers that no longer appear on their source URL.

This script is read-only. It compares Website offers in promo_offer_master with
current promo_website_staging.page_content for the same normalized source_url.
"""
from __future__ import annotations

import argparse
import csv
import json
import os
import re
import sys
from collections import Counter
from dataclasses import dataclass
from datetime import date, datetime, timezone
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional

import requests
from dotenv import load_dotenv

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from utils.offer_evidence_segments import normalize_segment_text, normalize_url, split_page_content

OUTPUT_DIR = PROJECT_ROOT / "output" / "results"
REPORT_DIR = PROJECT_ROOT / "reports"
PAGE_SIZE = 1000

MASTER_TABLE = "promo_offer_master"
STAGING_TABLE = "promo_website_staging"

MASTER_SELECT = (
    "id,channel,source_url,source_name,template_type,service_category,service_name,"
    "offer_raw_text,end_date,discount_percent,discount_amount,offer_content,"
    "regular_price,discount_price,membership_price,unit_type,membership_name,"
    "created_at,business_id,moderation_status,status"
)
STAGING_SELECT = "promo_website_id,subpage_url,domain_name,page_content,crawl_timestamp,last_updated_at,processed_status,business_id"

UNVERIFIABLE_PAGE_PATTERNS = [
    re.compile(r"checking the site connection security", re.IGNORECASE),
    re.compile(r"requires cookies to be enabled", re.IGNORECASE),
    re.compile(r"enable javascript", re.IGNORECASE),
    re.compile(r"access denied", re.IGNORECASE),
    re.compile(r"forbidden", re.IGNORECASE),
    re.compile(r"captcha", re.IGNORECASE),
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
        response = self.session.get(f"{self.base_url}/{table}", params=params, timeout=90)
        response.raise_for_status()
        return response.json()


@dataclass
class PageSnapshot:
    promo_website_id: Any
    source_url: str
    source_url_normalized: str
    domain_name: str
    content: str
    content_normalized: str
    segment_texts: List[str]
    crawl_timestamp: str
    last_updated_at: str
    processed_status: str


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Audit active Website offers missing from their source URL")
    parser.add_argument("--limit", type=int, default=None, help="Only audit first N master rows")
    parser.add_argument("--domain", default=None, help="Filter source_url/source_name by domain substring")
    parser.add_argument("--include-ended", action="store_true", help="Also audit rows whose status is not active")
    parser.add_argument("--output-dir", default=str(OUTPUT_DIR), help="Structured output directory")
    parser.add_argument("--report-dir", default=str(REPORT_DIR), help="Markdown report directory")
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
    filters: Optional[Dict[str, str]] = None,
    limit: Optional[int] = None,
    order: str,
) -> List[Dict[str, Any]]:
    rows: List[Dict[str, Any]] = []
    offset = 0
    while True:
        batch_limit = PAGE_SIZE
        if limit is not None:
            remaining = limit - len(rows)
            if remaining <= 0:
                break
            batch_limit = min(batch_limit, remaining)
        batch = client.fetch_rows(
            table,
            select,
            filters=filters,
            limit=batch_limit,
            offset=offset,
            order=order,
        )
        if not batch:
            break
        rows.extend(batch)
        if len(batch) < batch_limit:
            break
        offset += len(batch)
    return rows


def clean_text(value: Any) -> str:
    return re.sub(r"\s+", " ", str(value or "").strip())


def compact_text(value: Any) -> str:
    return re.sub(r"[^a-z0-9]+", "", normalize_segment_text(value))


def extract_numbers(*values: Any) -> List[str]:
    numbers: List[str] = []
    seen: set[str] = set()
    for value in values:
        if value in (None, ""):
            continue
        if isinstance(value, (int, float, Decimal)):
            candidates = [str(value)]
        else:
            candidates = re.findall(r"\d+(?:,\d{3})*(?:\.\d+)?", str(value))
        for raw in candidates:
            text = raw.replace(",", "")
            try:
                decimal = Decimal(text)
            except (InvalidOperation, ValueError):
                continue
            normalized = format_decimal(decimal)
            if normalized not in seen:
                seen.add(normalized)
                numbers.append(normalized)
    return numbers


def format_decimal(decimal: Decimal) -> str:
    text = format(decimal.normalize(), "f")
    if "." in text:
        text = text.rstrip("0").rstrip(".")
    return text or "0"


def number_variants(value: str) -> set[str]:
    try:
        decimal = Decimal(value)
    except InvalidOperation:
        return {value}
    variants = {format_decimal(decimal)}
    if decimal == decimal.to_integral():
        variants.add(str(int(decimal)))
        variants.add(f"{int(decimal)}.00")
    else:
        variants.add(f"{decimal:.2f}")
    return {item.rstrip("0").rstrip(".") if "." in item else item for item in variants} | variants


def parse_date(value: Any) -> Optional[date]:
    text = clean_text(value)
    if not text:
        return None
    for fmt in ("%Y-%m-%d", "%m/%d/%Y", "%m/%d/%y"):
        try:
            return datetime.strptime(text[:10], fmt).date()
        except ValueError:
            continue
    try:
        return datetime.fromisoformat(text.replace("Z", "+00:00")).date()
    except ValueError:
        return None


def token_set(value: Any) -> set[str]:
    normalized = normalize_segment_text(value)
    tokens = set(re.findall(r"[a-z0-9][a-z0-9+&/-]{1,}", normalized))
    stop = {
        "and",
        "the",
        "for",
        "with",
        "from",
        "price",
        "regular",
        "sale",
        "unit",
        "units",
        "treatment",
        "treatments",
        "membership",
        "member",
        "off",
    }
    return {token for token in tokens if token not in stop and not token.isdigit()}


def build_page_snapshots(rows: Iterable[Dict[str, Any]]) -> Dict[str, PageSnapshot]:
    snapshots: Dict[str, PageSnapshot] = {}
    for row in rows:
        source_url = clean_text(row.get("subpage_url"))
        normalized_url = normalize_url(source_url)
        content = clean_text(row.get("page_content"))
        segments = [clean_text(text) for _, text in split_page_content(content)]
        snapshot = PageSnapshot(
            promo_website_id=row.get("promo_website_id"),
            source_url=source_url,
            source_url_normalized=normalized_url,
            domain_name=clean_text(row.get("domain_name")),
            content=content,
            content_normalized=normalize_segment_text(content),
            segment_texts=segments,
            crawl_timestamp=clean_text(row.get("crawl_timestamp")),
            last_updated_at=clean_text(row.get("last_updated_at")),
            processed_status=clean_text(row.get("processed_status")),
        )
        existing = snapshots.get(normalized_url)
        if not existing or snapshot.last_updated_at > existing.last_updated_at:
            snapshots[normalized_url] = snapshot
    return snapshots


def is_unverifiable_snapshot(snapshot: PageSnapshot) -> bool:
    if not snapshot.content_normalized:
        return True
    if any(pattern.search(snapshot.content) for pattern in UNVERIFIABLE_PAGE_PATTERNS):
        return True
    price_hits = len(re.findall(r"\$\s*\d", snapshot.content))
    service_hits = len(
        re.findall(
            r"\b(botox|dysport|jeuveau|filler|restylane|juvederm|membership|special|promo|sale|price)\b",
            snapshot.content_normalized,
        )
    )
    return len(snapshot.content_normalized) < 500 and price_hits == 0 and service_hits <= 2


def offer_text_candidates(offer: Dict[str, Any]) -> List[str]:
    candidates = [
        offer.get("offer_raw_text"),
        offer.get("service_name"),
        offer.get("membership_name"),
        offer.get("template_type"),
    ]
    content = offer.get("offer_content")
    if isinstance(content, dict):
        candidates.extend(str(key) for key in content.keys())
    elif isinstance(content, list):
        candidates.extend(str(item) for item in content)
    elif content:
        candidates.append(content)
    return [clean_text(item) for item in candidates if clean_text(item)]


def best_segment_match(offer: Dict[str, Any], snapshot: PageSnapshot) -> Dict[str, Any]:
    raw = clean_text(offer.get("offer_raw_text"))
    service = clean_text(offer.get("service_name"))
    candidates = offer_text_candidates(offer)
    offer_tokens = token_set(" ".join(candidates))
    price_numbers = extract_numbers(
        offer.get("regular_price"),
        offer.get("discount_price"),
        offer.get("membership_price"),
        offer.get("discount_amount"),
        offer.get("discount_percent"),
        raw,
    )
    required_prices = [
        value
        for value in extract_numbers(
            offer.get("regular_price"),
            offer.get("discount_price"),
            offer.get("membership_price"),
            offer.get("discount_amount"),
        )
        if value != "0"
    ]

    page_compact = compact_text(snapshot.content)
    raw_compact = compact_text(raw)
    exact_raw_on_page = bool(raw_compact and len(raw_compact) >= 20 and raw_compact in page_compact)

    best = {
        "score": 0.0,
        "segment": "",
        "token_overlap": 0.0,
        "matched_prices": [],
        "missing_prices": required_prices,
        "reasons": [],
    }
    if exact_raw_on_page:
        best["score"] = 1.0
        best["segment"] = raw
        best["token_overlap"] = 1.0
        best["matched_prices"] = required_prices
        best["missing_prices"] = []
        best["reasons"].append("exact_offer_raw_text_present")
        return best

    for segment in snapshot.segment_texts or [snapshot.content]:
        segment_normalized = normalize_segment_text(segment)
        segment_tokens = token_set(segment)
        overlap = 0.0
        if offer_tokens:
            overlap = len(offer_tokens & segment_tokens) / max(1, len(offer_tokens))

        matched_prices = []
        missing_prices = []
        for price in required_prices:
            variants = number_variants(price)
            if any(re.search(rf"(?<!\d){re.escape(item)}(?!\d)", segment_normalized) for item in variants):
                matched_prices.append(price)
            else:
                missing_prices.append(price)

        service_present = bool(service and normalize_segment_text(service) in segment_normalized)
        raw_short_present = bool(raw and compact_text(raw) and compact_text(raw) in compact_text(segment))
        price_score = len(matched_prices) / max(1, len(required_prices)) if required_prices else 0.0
        score = overlap * 0.55 + price_score * 0.35 + (0.10 if service_present else 0.0)
        if raw_short_present:
            score = max(score, 0.9)

        if score > best["score"]:
            reasons = []
            if service_present:
                reasons.append("service_name_present")
            if matched_prices:
                reasons.append("price_values_present")
            if overlap >= 0.5:
                reasons.append("offer_tokens_overlap")
            best = {
                "score": round(score, 4),
                "segment": segment[:500],
                "token_overlap": round(overlap, 4),
                "matched_prices": matched_prices,
                "missing_prices": missing_prices,
                "reasons": reasons,
            }

    return best


def classify_offer(offer: Dict[str, Any], snapshot: Optional[PageSnapshot]) -> Dict[str, Any]:
    source_url = clean_text(offer.get("source_url"))
    normalized_url = normalize_url(source_url)
    end_date = parse_date(offer.get("end_date"))
    end_date_expired = bool(end_date and end_date < date.today())

    if not snapshot:
        return {
            "offer_id": offer.get("id"),
            "source_url": source_url,
            "source_url_normalized": normalized_url,
            "matched_promo_website_id": "",
            "source_name": clean_text(offer.get("source_name")),
            "service_name": clean_text(offer.get("service_name")),
            "offer_raw_text": clean_text(offer.get("offer_raw_text")),
            "status": clean_text(offer.get("status")),
            "end_date": clean_text(offer.get("end_date")),
            "verdict": "no_current_staging_page",
            "confidence": 0.75,
            "score": 0,
            "reasons": "source_url has no matching promo_website_staging row",
            "matched_prices": "",
            "missing_prices": ",".join(extract_numbers(offer.get("regular_price"), offer.get("discount_price"), offer.get("membership_price"))),
            "matched_segment": "",
            "page_last_updated_at": "",
            "page_crawl_timestamp": "",
            "page_verification_quality": "missing_staging_page",
        }

    if is_unverifiable_snapshot(snapshot):
        return {
            "offer_id": offer.get("id"),
            "source_url": source_url,
            "source_url_normalized": normalized_url,
            "matched_promo_website_id": snapshot.promo_website_id,
            "source_name": clean_text(offer.get("source_name")),
            "service_name": clean_text(offer.get("service_name")),
            "offer_raw_text": clean_text(offer.get("offer_raw_text")),
            "status": clean_text(offer.get("status")),
            "end_date": clean_text(offer.get("end_date")),
            "verdict": "source_page_unverifiable",
            "confidence": 0.4,
            "score": 0,
            "reasons": "current staging content is blocked, empty, or degraded",
            "matched_prices": "",
            "missing_prices": ",".join(extract_numbers(offer.get("regular_price"), offer.get("discount_price"), offer.get("membership_price"))),
            "matched_segment": snapshot.content[:500],
            "page_last_updated_at": snapshot.last_updated_at,
            "page_crawl_timestamp": snapshot.crawl_timestamp,
            "page_verification_quality": "unverifiable_content",
        }

    match = best_segment_match(offer, snapshot)
    score = float(match["score"])
    reasons = list(match["reasons"])
    if end_date_expired:
        reasons.append("end_date_in_past")

    if score >= 0.82:
        verdict = "present_on_source"
        confidence = 0.9
    elif score >= 0.55:
        verdict = "likely_present_on_source"
        confidence = 0.65
    elif end_date_expired:
        verdict = "expired_by_date_and_missing_from_source"
        confidence = 0.9
    elif score <= 0.2 and match["missing_prices"]:
        verdict = "likely_missing_from_source"
        confidence = 0.78
    else:
        verdict = "needs_review"
        confidence = 0.55

    return {
        "offer_id": offer.get("id"),
        "source_url": source_url,
        "source_url_normalized": normalized_url,
        "matched_promo_website_id": snapshot.promo_website_id,
        "source_name": clean_text(offer.get("source_name")),
        "service_name": clean_text(offer.get("service_name")),
        "offer_raw_text": clean_text(offer.get("offer_raw_text")),
        "status": clean_text(offer.get("status")),
        "end_date": clean_text(offer.get("end_date")),
        "verdict": verdict,
        "confidence": confidence,
        "score": score,
        "reasons": ";".join(reasons),
        "matched_prices": ",".join(match["matched_prices"]),
        "missing_prices": ",".join(match["missing_prices"]),
        "matched_segment": match["segment"],
        "page_last_updated_at": snapshot.last_updated_at,
        "page_crawl_timestamp": snapshot.crawl_timestamp,
        "page_verification_quality": "verifiable_content",
    }


def write_outputs(output_dir: Path, report_dir: Path, rows: List[Dict[str, Any]]) -> Dict[str, str]:
    output_dir.mkdir(parents=True, exist_ok=True)
    report_dir.mkdir(parents=True, exist_ok=True)
    timestamp = datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S")
    csv_path = output_dir / f"promo_offer_master_source_presence_audit_{timestamp}.csv"
    json_path = output_dir / f"promo_offer_master_source_presence_audit_{timestamp}.json"
    report_path = report_dir / f"promo_offer_master_source_presence_audit_{timestamp}.md"

    fieldnames = [
        "offer_id",
        "verdict",
        "confidence",
        "score",
        "source_name",
        "service_name",
        "source_url",
        "matched_promo_website_id",
        "status",
        "end_date",
        "reasons",
        "matched_prices",
        "missing_prices",
        "offer_raw_text",
        "matched_segment",
        "page_last_updated_at",
        "page_crawl_timestamp",
        "page_verification_quality",
    ]
    with csv_path.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=fieldnames, extrasaction="ignore")
        writer.writeheader()
        writer.writerows(rows)

    counts = Counter(row["verdict"] for row in rows)
    missing = [row for row in rows if row["verdict"] in {"likely_missing_from_source", "expired_by_date_and_missing_from_source"}]
    payload = {
        "summary": {
            "generated_at": datetime.now(timezone.utc).isoformat(),
            "offers_audited": len(rows),
            "verdict_counts": dict(counts),
            "likely_expired_or_missing": len(missing),
        },
        "rows": rows,
    }
    json_path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")

    top_missing = sorted(missing, key=lambda row: (-float(row["confidence"]), row["source_name"], row["offer_id"]))[:50]
    lines = [
        "# promo_offer_master Source Presence Audit",
        "",
        f"Generated at: {payload['summary']['generated_at']}",
        f"Offers audited: {len(rows)}",
        "",
        "## Verdict Counts",
        "",
    ]
    for verdict, count in counts.most_common():
        lines.append(f"- {verdict}: {count}")
    lines.extend(["", "## Likely Expired Or Missing From Source", ""])
    for row in top_missing:
        raw = row["offer_raw_text"][:180].replace("\n", " ")
        lines.append(
            f"- #{row['offer_id']} `{row['source_name']}` `{row['service_name']}` "
            f"{row['verdict']} confidence={row['confidence']} score={row['score']} "
            f"missing_prices={row['missing_prices']} url={row['source_url']} - {raw}"
        )
    report_path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return {"csv": str(csv_path), "json": str(json_path), "report": str(report_path)}


def main() -> None:
    args = parse_args()
    client = load_client()
    master_filters = {"channel": "eq.Website"}
    if not args.include_ended:
        master_filters["status"] = "eq.active"
    master_rows = fetch_all_rows(
        client,
        MASTER_TABLE,
        MASTER_SELECT,
        filters=master_filters,
        limit=args.limit,
        order="id.asc",
    )
    if args.domain:
        needle = args.domain.lower()
        master_rows = [
            row for row in master_rows
            if needle in clean_text(row.get("source_url")).lower() or needle in clean_text(row.get("source_name")).lower()
        ]
    staging_rows = fetch_all_rows(client, STAGING_TABLE, STAGING_SELECT, order="promo_website_id.asc")
    snapshots = build_page_snapshots(staging_rows)
    audited = [
        classify_offer(row, snapshots.get(normalize_url(row.get("source_url"))))
        for row in master_rows
    ]
    paths = write_outputs(Path(args.output_dir), Path(args.report_dir), audited)
    counts = Counter(row["verdict"] for row in audited)
    print(
        json.dumps(
            {
                "status": "ok",
                "offers_audited": len(audited),
                "verdict_counts": dict(counts),
                "likely_expired_or_missing": sum(
                    counts[item]
                    for item in ("likely_missing_from_source", "expired_by_date_and_missing_from_source")
                ),
                "paths": paths,
            },
            ensure_ascii=False,
        )
    )


if __name__ == "__main__":
    main()
