#!/usr/bin/env python3
"""Repair only the remaining evidence/duplicate findings from a QA CSV.

The command is intentionally dry-run by default.  It persists raw evidence
only for a 2xx response with markdown, fills promotion content only from that
source markdown, and prunes a promotion only when it has no offers and its
source contains no extractable promotion segment.
"""
from __future__ import annotations

import argparse
import csv
import json
import os
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, Iterable, Mapping, Sequence

from dotenv import load_dotenv

PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from utils.agent_defense import audit_then_delete_rows, script_agent_name  # noqa: E402
from utils.clinic_promotions_db import refresh_promotion_content_from_markdown  # noqa: E402
from utils.firecrawl_client import scrape_page_markdown_v2  # noqa: E402
from utils.firecrawl_scrape_raw_db import canonical_scrape_url, save_scrape_response, scrape_request_fingerprint  # noqa: E402
from utils.recent_raw_extraction import extract_promotion_content_from_markdown  # noqa: E402
from utils.schema_contract import (  # noqa: E402
    CLINIC_MEMBERSHIP_SELECT,
    CLINIC_PROMOTION_SELECT,
    CLINIC_SERVICE_SELECT,
    TABLE_CLINIC_MEMBERSHIPS,
    TABLE_CLINIC_PROMOTIONS,
    TABLE_CLINIC_SERVICES,
    TABLE_PROMO_OFFER_MASTER,
)
from utils.supabase_rest import SupabaseRestClient, get_supabase_writer_key  # noqa: E402

ROW_CONFIG = {
    TABLE_CLINIC_SERVICES: ("service_id", CLINIC_SERVICE_SELECT),
    TABLE_CLINIC_MEMBERSHIPS: ("plan_id", CLINIC_MEMBERSHIP_SELECT),
}


def is_2xx(response: Mapping[str, Any]) -> bool:
    """Require Firecrawl's resolved HTTP status, never assume success."""
    data = response.get("data") if isinstance(response.get("data"), Mapping) else response
    metadata = data.get("metadata") if isinstance(data, Mapping) else None
    candidates = (
        (metadata or {}).get("status_code") if isinstance(metadata, Mapping) else None,
        (metadata or {}).get("statusCode") if isinstance(metadata, Mapping) else None,
        data.get("status_code") if isinstance(data, Mapping) else None,
        data.get("statusCode") if isinstance(data, Mapping) else None,
    )
    for candidate in candidates:
        try:
            return 200 <= int(candidate) < 300
        except (TypeError, ValueError):
            continue
    return bool(response.get("success", True))


def issue_rows(path: Path, kinds: set[str]) -> list[dict[str, str]]:
    with path.open(encoding="utf-8-sig", newline="") as handle:
        return [row for row in csv.DictReader(handle) if row.get("issue_type") in kinds]


def source_url_from_issue(row: Mapping[str, str]) -> str:
    detail = str(row.get("detail") or "")
    return detail.rsplit(": ", 1)[-1].strip() if ": " in detail else ""


def lineage_targets(rows: Iterable[Mapping[str, str]]) -> list[dict[str, str]]:
    targets: dict[tuple[str, str, str], dict[str, str]] = {}
    for row in rows:
        table, row_id, source_url = row.get("table", ""), row.get("id", ""), source_url_from_issue(row)
        if table not in ROW_CONFIG or not row_id or not source_url:
            continue
        key = (table, row_id, canonical_scrape_url(source_url))
        targets[key] = {"table": table, "id": row_id, "source_url": source_url}
    return list(targets.values())


def load_client() -> SupabaseRestClient:
    load_dotenv(PROJECT_ROOT / ".env")
    base_url, key = os.getenv("SUPABASE_URL"), get_supabase_writer_key()
    if not base_url or not key:
        raise RuntimeError("缺少 SUPABASE_URL 或 SUPABASE_WRITER_KEY")
    return SupabaseRestClient(base_url, key)


def current_source_matches(client: Any, target: Mapping[str, str]) -> bool:
    id_field, select = ROW_CONFIG[target["table"]]
    rows = client.fetch_rows(target["table"], select, filters={id_field: f"eq.{target['id']}"}, limit=1)
    return bool(rows) and canonical_scrape_url(str(rows[0].get("source_url") or "")) == canonical_scrape_url(target["source_url"])


def scrape_and_persist(client: Any, _firecrawl: Any, source_url: str, *, apply: bool) -> dict[str, Any]:
    result: dict[str, Any] = {"source_url": source_url, "applied": False}
    try:
        markdown, response = scrape_page_markdown_v2(source_url)
    except Exception as exc:
        result.update(status="scrape_failed", error=str(exc)[:500])
        return result
    if not is_2xx(response):
        result.update(status="non_2xx")
        return result
    if not markdown.strip():
        result.update(status="empty_markdown")
        return result
    result.update(status="ready", markdown=markdown, response=response)
    if apply:
        save_scrape_response(
            client,
            scrape_request_fingerprint(source_url),
            source_url,
            response,
            success=True,
        )
        result["applied"] = True
    return result


def repair_lineage(client: Any, firecrawl: Any, targets: Sequence[Mapping[str, str]], *, apply: bool) -> list[dict[str, Any]]:
    cache: dict[str, dict[str, Any]] = {}
    results: list[dict[str, Any]] = []
    for target in targets:
        result: dict[str, Any] = {**target, "applied": False}
        if not current_source_matches(client, target):
            result["status"] = "precondition_failed"
        else:
            key = canonical_scrape_url(target["source_url"])
            evidence = cache.setdefault(key, scrape_and_persist(client, firecrawl, target["source_url"], apply=apply))
            result.update({k: v for k, v in evidence.items() if k not in {"markdown", "response"}})
        results.append(result)
    return results


def promotion_has_offers(client: Any, promotion_id: str) -> bool:
    return bool(client.fetch_rows(TABLE_PROMO_OFFER_MASTER, "id", filters={"promotion_id": f"eq.{promotion_id}"}, limit=1))


def repair_promotions(client: Any, firecrawl: Any, ids: Sequence[str], *, apply: bool, prune: bool) -> list[dict[str, Any]]:
    results: list[dict[str, Any]] = []
    for promotion_id in ids:
        rows = client.fetch_rows(TABLE_CLINIC_PROMOTIONS, CLINIC_PROMOTION_SELECT, filters={"promotion_id": f"eq.{promotion_id}"}, limit=1)
        result: dict[str, Any] = {"promotion_id": promotion_id, "applied": False}
        if not rows or rows[0].get("promotion_content"):
            result["status"] = "precondition_failed"
            results.append(result)
            continue
        promotion = rows[0]
        evidence = scrape_and_persist(client, firecrawl, str(promotion.get("source_url") or ""), apply=apply)
        result.update({k: v for k, v in evidence.items() if k not in {"markdown", "response"}})
        markdown = str(evidence.get("markdown") or "")
        segments = extract_promotion_content_from_markdown(markdown) if markdown else []
        if segments:
            refreshed = refresh_promotion_content_from_markdown(client, promotion, markdown, dry_run=not apply)
            result.update(status="content_ready", segments=len(segments), applied=bool(refreshed.get("updated") and apply))
        elif evidence.get("status") == "ready" and not promotion_has_offers(client, promotion_id):
            result["status"] = "prune_candidate"
            if apply and prune:
                audit_then_delete_rows(
                    client,
                    TABLE_CLINIC_PROMOTIONS,
                    {"promotion_id": f"eq.{promotion_id}"},
                    applied_by=f"agent:{script_agent_name()}",
                    reason="soft_delete: prune unreferenced empty promotion",
                )
                result.update(status="pruned", applied=True)
        elif evidence.get("status") == "ready":
            result["status"] = "no_extractable_content_with_offers"
        results.append(result)
    return results


def merge_duplicate_promotions(client: Any, rows: Sequence[Mapping[str, str]], *, apply: bool) -> list[dict[str, Any]]:
    results: list[dict[str, Any]] = []
    for row in rows:
        ids = [value.strip() for value in str(row.get("id") or "").split(",") if value.strip()]
        if len(ids) != 2:
            results.append({"ids": ids, "status": "unsupported_duplicate_group", "applied": False})
            continue
        promotions = client.fetch_rows(TABLE_CLINIC_PROMOTIONS, CLINIC_PROMOTION_SELECT, filters={"promotion_id": f"in.({','.join(ids)})"}, limit=2)
        if len(promotions) != 2:
            results.append({"ids": ids, "status": "precondition_failed", "applied": False})
            continue
        survivor, duplicate = sorted(promotions, key=lambda item: int(item["promotion_id"]))
        compatible = (
            survivor.get("business_id") == duplicate.get("business_id")
            and canonical_scrape_url(str(survivor.get("source_url") or "")) == canonical_scrape_url(str(duplicate.get("source_url") or ""))
            and (survivor.get("promotion_content") or []) == (duplicate.get("promotion_content") or [])
        )
        result = {"survivor": survivor["promotion_id"], "duplicate": duplicate["promotion_id"], "applied": False}
        if not compatible:
            result["status"] = "precondition_failed"
        else:
            refs = client.fetch_rows(TABLE_PROMO_OFFER_MASTER, "id", filters={"promotion_id": f"eq.{duplicate['promotion_id']}"}, limit=1000)
            result.update(status="ready", offer_refs=len(refs))
            if apply:
                if refs:
                    client.update_row(TABLE_PROMO_OFFER_MASTER, {"promotion_id": f"eq.{duplicate['promotion_id']}"}, {"promotion_id": survivor["promotion_id"]})
                audit_then_delete_rows(
                    client,
                    TABLE_CLINIC_PROMOTIONS,
                    {"promotion_id": f"eq.{duplicate['promotion_id']}"},
                    applied_by=f"agent:{script_agent_name()}",
                    reason="soft_delete: merge duplicate promotion",
                )
                result["applied"] = True
        results.append(result)
    return results


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--issues-csv", type=Path, required=True)
    parser.add_argument("--apply", action="store_true")
    parser.add_argument("--prune-unreferenced-empty-promotions", action="store_true")
    parser.add_argument("--result-dir", type=Path, default=PROJECT_ROOT / "output" / "results")
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    client, firecrawl = load_client(), None
    lineage_issues = issue_rows(args.issues_csv, {"missing_raw_lineage", "source_url_not_in_raw"})
    promotion_issues = issue_rows(args.issues_csv, {"missing_promotion_content"})
    duplicate_issues = issue_rows(args.issues_csv, {"duplicate_promotion_url"})
    lineage = repair_lineage(client, firecrawl, lineage_targets(lineage_issues), apply=args.apply)
    promotions = repair_promotions(client, firecrawl, [row["id"] for row in promotion_issues], apply=args.apply, prune=args.prune_unreferenced_empty_promotions)
    duplicates = merge_duplicate_promotions(client, duplicate_issues, apply=args.apply)
    payload = {"dry_run": not args.apply, "lineage": lineage, "promotions": promotions, "duplicates": duplicates}
    args.result_dir.mkdir(parents=True, exist_ok=True)
    path = args.result_dir / f"remaining_extraction_quality_repair_{datetime.now(timezone.utc):%Y%m%d_%H%M%S}.json"
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2, default=str), encoding="utf-8")
    print(json.dumps({"result_path": str(path), "lineage": len(lineage), "promotions": len(promotions), "duplicates": len(duplicates)}, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
