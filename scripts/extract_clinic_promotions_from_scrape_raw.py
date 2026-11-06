#!/usr/bin/env python3
"""Extract page-level clinic promotions from a dated scrape-raw batch.

The command is intentionally dry-run by default.  ``--apply`` is required before
the script will insert or update ``clinic_promotions`` rows.
"""
from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path
from typing import Any, Mapping, Optional, Sequence

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from utils.promo_paths import sanitized_output_path as _output_path
from utils.promo_extraction import llm_extract_promotions, merge_promotion_items
from utils.recent_raw_extraction import (
    promotion_evidence_markdown,
    resolve_business,
)
from utils.scrape_raw_promotion_llm import (
    ProxySafeOpenAICompatibleClient,
    normalize_llm_api_url,
    resolve_local_llm_api_key,
)
from utils.scrape_raw_promotion_loading import (
    _load_all_businesses,
    _metadata_text,
    load_batch_scrapes,
)
from utils.scrape_raw_promotion_persistence import persist_page_promotion
from utils.supabase_rest import SupabaseRestClient, get_supabase_writer_key


DEFAULT_BATCH_DATE = "2026-07-30"
DEFAULT_TIMEZONE = "Asia/Shanghai"
DEFAULT_LLM_BASE_URL = "http://192.168.1.189:1234/v1"
DEFAULT_LLM_MODEL = "schematron-8b"


def run(
    *,
    client: Any,
    llm: Any,
    batch_date: str = DEFAULT_BATCH_DATE,
    timezone_name: str = DEFAULT_TIMEZONE,
    raw_ids: Optional[Sequence[int]] = None,
    limit: Optional[int] = None,
    apply: bool = False,
    schema: Optional[dict[str, Any]] = None,
    rendered_visibility: Mapping[str, Mapping[str, Any]] | None = None,
) -> dict[str, Any]:
    """Run the isolated page-by-page extraction and return a reconciliation audit."""
    extraction_schema = schema or json.loads(
        (PROJECT_ROOT / "schema" / "promotion_extraction_schema.json").read_text(
            encoding="utf-8"
        )
    )
    scrapes = load_batch_scrapes(
        client,
        batch_date=batch_date,
        timezone_name=timezone_name,
        raw_ids=raw_ids,
        limit=limit,
    )
    businesses = _load_all_businesses(client)
    # Host ambiguity is decided from master_business_info matches. Promo slugs such
    # as summer/fall/winter-specials must not be mistaken for location paths.
    multilocation_hosts: set[str] = set()

    results: list[dict[str, Any]] = []
    actions: dict[str, int] = {}
    accepted_count = 0
    gate_rejected_count = 0
    error_count = 0
    extracted_item_count = 0

    for row in scrapes:
        raw_id = row.get("id")
        source_url = str(row.get("source_url") or "")
        evidence = promotion_evidence_markdown(row)
        source = {
            "url": source_url,
            "title": _metadata_text(row, "title"),
            "description": _metadata_text(row, "description"),
            "text": evidence,
        }
        gate = resolve_business(source, businesses, multilocation_hosts)
        if not gate.accepted or gate.business_id is None:
            gate_rejected_count += 1
            results.append(
                {
                    "raw_id": raw_id,
                    "source_url": source_url,
                    "status": "business_gate_rejected",
                    "reason": gate.reason,
                }
            )
            continue
        try:
            payload = llm_extract_promotions(
                llm,
                source_url=source_url,
                evidence=evidence,
                schema=extraction_schema,
            )
            items = payload.get("promotions") or []
            extracted_item_count += len(items)
            if not items:
                results.append(
                    {
                        "raw_id": raw_id,
                        "business_id": gate.business_id,
                        "source_url": source_url,
                        "status": "no_promotions",
                    }
                )
                continue
            merged = merge_promotion_items(
                items,
                evidence,
                webpage_title=_metadata_text(row, "title"),
                source_url=source_url,
            )
            persistence = persist_page_promotion(
                client,
                business_id=gate.business_id,
                source_url=source_url,
                item=merged,
                apply=apply,
                rendered_visibility=(rendered_visibility or {}).get(source_url),
            )
            action = str(persistence.get("action") or "unknown")
            actions[action] = actions.get(action, 0) + 1
            if persistence.get("accepted"):
                accepted_count += 1
            results.append(
                {
                    "raw_id": raw_id,
                    "business_id": gate.business_id,
                    "source_url": source_url,
                    "status": "accepted" if persistence.get("accepted") else "rejected",
                    "promotions_extracted": len(items),
                    "promotion_title": merged.get("promotion_title"),
                    "promotion_content": merged.get("promotion_content") or [],
                    **persistence,
                }
            )
        except ValueError as exc:
            if str(exc) == "no validated promotions":
                results.append(
                    {
                        "raw_id": raw_id,
                        "business_id": gate.business_id,
                        "source_url": source_url,
                        "status": "rejected",
                        "reason": "no_validated_promotions",
                    }
                )
                continue
            error_count += 1
            results.append(
                {
                    "raw_id": raw_id,
                    "business_id": gate.business_id,
                    "source_url": source_url,
                    "status": "error",
                    "error_type": type(exc).__name__,
                    "error": str(exc)[:1000],
                }
            )
        except Exception as exc:
            error_count += 1
            results.append(
                {
                    "raw_id": raw_id,
                    "business_id": gate.business_id,
                    "source_url": source_url,
                    "status": "error",
                    "error_type": type(exc).__name__,
                    "error": str(exc)[:1000],
                }
            )

    return {
        "apply": apply,
        "batch_date": batch_date,
        "timezone": timezone_name,
        "raw_ids": list(raw_ids or []),
        "limit": limit,
        "reconciliation": {
            "scrapes_loaded": len(scrapes),
            "results": len(results),
            "business_gate_rejected": gate_rejected_count,
            "promotion_items_extracted": extracted_item_count,
            "page_promotions_accepted": accepted_count,
            "errors": error_count,
            "actions": actions,
            "balanced": len(results) == len(scrapes),
        },
        "results": results,
    }


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--apply", action="store_true", help="write accepted rows")
    parser.add_argument("--batch_date", default=DEFAULT_BATCH_DATE)
    parser.add_argument("--timezone", default=DEFAULT_TIMEZONE)
    parser.add_argument("--raw_id", action="append", type=int, default=[])
    parser.add_argument("--limit", type=int, default=None)
    parser.add_argument(
        "--visibility-manifest",
        type=Path,
        default=None,
        help="Rendered visibility report produced by the ego browser gate; required with --apply",
    )
    parser.add_argument("--llm_base_url", default=DEFAULT_LLM_BASE_URL)
    parser.add_argument("--llm_model", default=DEFAULT_LLM_MODEL)
    parser.add_argument(
        "--output",
        default=str(
            PROJECT_ROOT
            / ".firecrawl"
            / "master-business-search"
            / "clinic_promotions_scrape_raw_extract_audit.json"
        ),
    )
    args = parser.parse_args()

    from dotenv import load_dotenv

    load_dotenv(PROJECT_ROOT / ".env")
    supabase_url = os.getenv("SUPABASE_URL", "").strip()
    if not supabase_url:
        raise RuntimeError("Missing SUPABASE_URL")
    client = SupabaseRestClient(supabase_url, get_supabase_writer_key())
    if args.apply and args.visibility_manifest is None:
        raise RuntimeError("--visibility-manifest is required with --apply")
    rendered_visibility: Mapping[str, Mapping[str, Any]] = {}
    if args.visibility_manifest is not None:
        payload = json.loads(args.visibility_manifest.read_text(encoding="utf-8"))
        rendered_visibility = payload.get("results", payload)
    llm = ProxySafeOpenAICompatibleClient(
        api_url=normalize_llm_api_url(args.llm_base_url),
        api_key=resolve_local_llm_api_key(os.environ),
        model=args.llm_model,
    )
    audit = run(
        client=client,
        llm=llm,
        batch_date=args.batch_date,
        timezone_name=args.timezone,
        raw_ids=tuple(args.raw_id) or None,
        limit=args.limit,
        apply=args.apply,
        rendered_visibility=rendered_visibility,
    )
    output = _output_path(args.output)
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps(audit, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps(audit, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
