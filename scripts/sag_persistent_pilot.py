#!/usr/bin/env python3
"""Build and verify the persistent structured SAG pilot in Supabase."""
from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime, timezone
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from scripts.sag_multi_retrieval_eval import load_city_rows
from utils.sag_persistent import (
    SagPilotRepository,
    build_pilot_clients,
    extract_document_events,
    extract_query_entities,
)
from utils.sag_structured_corpus import build_structured_documents
from utils.supabase_access import load_supabase_client


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--city", default="Tucson")
    parser.add_argument("--clinic-limit", type=int, default=20)
    parser.add_argument("--query", default="哪些诊所同时有 Botox 服务和会员优惠？")
    parser.add_argument("--top-k", type=int, default=5)
    parser.add_argument("--llm-model", default="schematron-8b")
    parser.add_argument("--embedding-model", default="text-embedding-nomic-embed-text-v1.5")
    parser.add_argument("--llm-base-url", default="http://192.168.1.189:1234/v1")
    parser.add_argument("--embedding-base-url", default="http://192.168.1.189:1234/v1")
    parser.add_argument("--local-api-key", default="local", help="Optional local OpenAI-compatible API key")
    parser.add_argument("--result-dir", type=Path, default=PROJECT_ROOT / "output" / "results")
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    if args.clinic_limit <= 0 or args.top_k <= 0:
        raise ValueError("--clinic-limit and --top-k must be positive")
    repository = SagPilotRepository.from_env()
    repository.validate_connection()
    services, memberships, promotions = load_city_rows(
        load_supabase_client(PROJECT_ROOT), city=args.city, clinic_limit=args.clinic_limit
    )
    documents = build_structured_documents(
        services=services, memberships=memberships, promotions=promotions
    )
    if not documents:
        raise RuntimeError(f"No structured records found for city={args.city!r}")
    llm, embedding = build_pilot_clients(
        llm_model=args.llm_model,
        embedding_model=args.embedding_model,
        llm_base_url=args.llm_base_url,
        embedding_base_url=args.embedding_base_url,
        api_key=args.local_api_key,
    )
    version_id = repository.ensure_index_version(
        llm_model=args.llm_model, embedding_model=args.embedding_model
    )
    changed = repository.upsert_documents(version_id, documents)
    results: list[dict] = []
    for document_id, document in changed:
        repository.mark_job(document_id, status="running")
        try:
            events = extract_document_events(llm, document)
            repository.persist_document_result(
                index_version_id=version_id,
                document_id=document_id,
                document=document,
                events=events,
                embedding_client=embedding,
                embedding_model=args.embedding_model,
            )
            results.append({"document_id": document_id, "status": "succeeded", "event_count": len(events)})
        except Exception as exc:
            repository.mark_job(document_id, status="failed", error=str(exc)[:4000])
            results.append({"document_id": document_id, "status": "failed", "error": str(exc)})
    query_vector = embedding.embed(args.query)
    vector_results = repository.vector_search(index_version_id=version_id, vector=query_vector, top_k=args.top_k)
    query_entities = extract_query_entities(llm, args.query)
    entity_vectors = [embedding.embed(entity) for entity in query_entities]
    multi_results = repository.multi_search(index_version_id=version_id, vectors=entity_vectors, top_k=args.top_k)
    repository.finish_index(version_id)
    report = {
        "index_version_id": version_id,
        "city": args.city,
        "models": {"llm": args.llm_model, "embedding": args.embedding_model},
        "record_counts": {"services": len(services), "memberships": len(memberships), "promotions": len(promotions), "documents": len(documents)},
        "indexed": results,
        "query": args.query,
        "query_entities": query_entities,
        "vector": vector_results,
        "multi": multi_results,
    }
    args.result_dir.mkdir(parents=True, exist_ok=True)
    path = args.result_dir / f"sag_persistent_pilot_{datetime.now(timezone.utc).strftime('%Y%m%d_%H%M%S')}.json"
    path.write_text(json.dumps(report, ensure_ascii=False, indent=2, default=str), encoding="utf-8")
    print(json.dumps({"result_path": str(path), "index_version_id": version_id, "changed_documents": len(changed), "failed_documents": sum(row["status"] == "failed" for row in results)}, ensure_ascii=False))
    return 0 if not any(row["status"] == "failed" for row in results) else 1


if __name__ == "__main__":
    raise SystemExit(main())
