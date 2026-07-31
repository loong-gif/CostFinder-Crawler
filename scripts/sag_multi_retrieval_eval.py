#!/usr/bin/env python3
"""Evaluate SAG multi retrieval over current structured clinic records only."""
from __future__ import annotations

import argparse
import asyncio
import json
import shutil
import sys
import tempfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from utils.sag_knowledge_engine import SagKnowledgeEngine
from utils.sag_structured_corpus import build_structured_documents, materialize_documents
from utils.schema_contract import (
    CLINIC_MEMBERSHIP_SELECT,
    CLINIC_PROMOTION_SELECT,
    CLINIC_SERVICE_SELECT,
    TABLE_CLINIC_MEMBERSHIPS,
    TABLE_CLINIC_PROMOTIONS,
    TABLE_CLINIC_SERVICES,
)
from utils.supabase_access import fetch_all_rows, load_supabase_client


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--city", default="Tucson")
    parser.add_argument("--clinic-limit", type=int, default=20)
    parser.add_argument("--top-k", type=int, default=5)
    parser.add_argument("--strategies", default="vector,multi")
    parser.add_argument("--query", default="", help="Optional single query to execute after indexing")
    parser.add_argument("--llm-model", default="gemini-3.5-flash-lite")
    parser.add_argument("--embedding-model", default="text-embedding-004")
    parser.add_argument("--result-dir", type=Path, default=PROJECT_ROOT / "output" / "results")
    return parser.parse_args()


def _in_filter(ids: list[int]) -> dict[str, str]:
    return {"business_id": "in.(%s)" % ",".join(str(item) for item in ids)}


def load_city_rows(client: Any, *, city: str, clinic_limit: int) -> tuple[list[dict], list[dict], list[dict]]:
    businesses = fetch_all_rows(
        client,
        "master_business_info",
        "business_id",
        filters={"city": f"eq.{city}"},
        order="business_id.asc",
        limit=clinic_limit,
    )
    business_ids = [int(row["business_id"]) for row in businesses if row.get("business_id") is not None]
    if not business_ids:
        return [], [], []
    filters = _in_filter(business_ids)
    return (
        fetch_all_rows(client, TABLE_CLINIC_SERVICES, CLINIC_SERVICE_SELECT, filters=filters),
        fetch_all_rows(client, TABLE_CLINIC_MEMBERSHIPS, CLINIC_MEMBERSHIP_SELECT, filters=filters),
        fetch_all_rows(client, TABLE_CLINIC_PROMOTIONS, CLINIC_PROMOTION_SELECT, filters=filters),
    )


async def run_sag(args: argparse.Namespace, documents_dir: Path, documents: list[Any]) -> dict[str, Any]:
    manifest: dict[str, dict[str, Any]] = {}
    ingests: list[dict[str, Any]] = []
    searches: dict[str, list[dict[str, Any]]] = {}
    materialized = materialize_documents(documents, documents_dir)
    async with SagKnowledgeEngine(
        data_dir=documents_dir.parent / "index",
        auto_extract=False,
        llm_model=args.llm_model,
        embedding_model=args.embedding_model,
    ) as engine:
        for item in materialized:
            result = await engine.ingest_markdown_file(item.path, extract=True)
            source_id = str(result.get("source_id") or "")
            if not source_id:
                raise RuntimeError(f"SAG did not return source_id for {item.path.name}")
            manifest[source_id] = {
                "record_type": item.document.record_type,
                "record_id": item.document.record_id,
                "business_id": item.document.business_id,
                "source_url": item.document.source_url,
                "updated_at": item.document.updated_at,
                "path": item.path.name,
            }
            ingests.append({**manifest[source_id], **result})
        if args.query:
            for strategy in [item.strip() for item in args.strategies.split(",") if item.strip()]:
                sections = await engine.search(args.query, strategy=strategy, top_k=args.top_k)
                searches[strategy] = [
                    {
                        **section,
                        "record": manifest.get(str(section.get("source_id") or "")),
                        "attributed": str(section.get("source_id") or "") in manifest,
                    }
                    for section in sections
                ]
    return {"manifest": manifest, "ingests": ingests, "searches": searches}


def main() -> int:
    args = parse_args()
    if args.clinic_limit <= 0 or args.top_k <= 0:
        raise ValueError("--clinic-limit and --top-k must be positive")
    client = load_supabase_client(PROJECT_ROOT)
    services, memberships, promotions = load_city_rows(client, city=args.city, clinic_limit=args.clinic_limit)
    documents = build_structured_documents(
        services=services, memberships=memberships, promotions=promotions
    )
    if not documents:
        raise RuntimeError(f"No structured SAG records found for city={args.city!r}")
    run_dir = Path(tempfile.mkdtemp(prefix="sag_structured_"))
    try:
        result = asyncio.run(run_sag(args, run_dir / "documents", documents))
        report = {
            "city": args.city,
            "models": {"llm": args.llm_model, "embedding": args.embedding_model},
            "query": args.query or None,
            "record_counts": {
                "clinic_services": len(services),
                "clinic_memberships": len(memberships),
                "clinic_promotions": len(promotions),
                "total": len(documents),
            },
            **result,
        }
        args.result_dir.mkdir(parents=True, exist_ok=True)
        stamp = datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S")
        report_path = args.result_dir / f"sag_multi_structured_{stamp}.json"
        report_path.write_text(json.dumps(report, ensure_ascii=False, indent=2, default=str), encoding="utf-8")
        print(json.dumps({"result_path": str(report_path), **report["record_counts"]}, ensure_ascii=False))
        return 0
    finally:
        shutil.rmtree(run_dir, ignore_errors=True)


if __name__ == "__main__":
    raise SystemExit(main())
