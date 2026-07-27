#!/usr/bin/env python3
"""Render evidence-backed clinic service audit records for table recovery."""
from __future__ import annotations

import argparse
import glob
import json
import sys
from pathlib import Path
from typing import Any

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from utils.clinic_services_mcp_sql import render_mcp_operations
from utils.offer_extraction_llm import canonicalize_service_name


def load_recovery_operations(pattern: str) -> list[dict[str, Any]]:
    """Keep the latest evidence payload for each business/service identity."""
    latest: dict[tuple[int, str], dict[str, Any]] = {}
    for filename in sorted(glob.glob(pattern)):
        audit = json.loads(Path(filename).read_text(encoding="utf-8"))
        for result in audit.get("results", []):
            for write in result.get("writes", []):
                operation = write.get("mcp_operation") or {}
                payload = operation.get("payload") or {}
                business_id = payload.get("business_id")
                service_name = str(payload.get("service_name") or "").strip()
                if business_id is None or not service_name:
                    continue
                normalized_payload = {
                    **payload,
                    "service_name": canonicalize_service_name(
                        service_name,
                        payload.get("service_name_raw"),
                    ),
                }
                # Restore always inserts idempotently: prior service IDs are invalid
                # after a destructive table reset.
                latest[(int(business_id), normalized_payload["service_name"].casefold())] = {
                    "action": "mcp_insert",
                    "payload": normalized_payload,
                }
    return [latest[key] for key in sorted(latest)]


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--pattern", default="output/results/clinic_services_firecrawl_audit_*.json")
    parser.add_argument("--chunk-size", type=int, default=40)
    parser.add_argument("--chunk-index", type=int, default=0)
    args = parser.parse_args()
    if args.chunk_size < 1 or args.chunk_index < 0:
        raise SystemExit("chunk-size must be positive and chunk-index must be non-negative")
    operations = load_recovery_operations(args.pattern)
    start = args.chunk_index * args.chunk_size
    chunk = operations[start:start + args.chunk_size]
    if not chunk:
        raise SystemExit(2)
    print(render_mcp_operations(chunk))


if __name__ == "__main__":
    main()
