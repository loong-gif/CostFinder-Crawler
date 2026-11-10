#!/usr/bin/env python3
"""Resume membership extraction from already captured Firecrawl pages."""
from __future__ import annotations

import argparse
import json
import sys
import time
from pathlib import Path

from dotenv import load_dotenv

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from utils.membership_repair_extraction import EXTRACTION_SCHEMA, extract_one
from utils.membership_repair_plan import build_dry_run
from utils.offer_extraction_llm import build_client_from_env


def read_json(path: Path):
    return json.loads(path.read_text(encoding="utf-8"))


def write_json(path: Path, payload) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--source-artifact-dir", type=Path, required=True)
    parser.add_argument("--artifact-dir", type=Path, required=True)
    parser.add_argument("--interval", type=float, default=5.0)
    parser.add_argument("--timeout", type=int, default=30)
    args = parser.parse_args()

    load_dotenv(PROJECT_ROOT / ".env")
    source_dir = args.source_artifact_dir
    snapshot = read_json(source_dir / "snapshot.json")
    pages = []
    for filename in ("firecrawl_main_scrapes.json", "firecrawl_fallback_scrapes.json"):
        pages.extend(read_json(source_dir / filename))
    pages = [page for page in pages if page.get("markdown")]
    llm = build_client_from_env()
    if llm is None:
        raise RuntimeError("LLM_API_URL/LLM_API_KEY/LLM_MODEL are required")
    if hasattr(llm, "timeout"):
        llm.timeout = max(1, int(args.timeout))

    args.artifact_dir.mkdir(parents=True, exist_ok=True)
    write_json(args.artifact_dir / "snapshot.json", snapshot)
    write_json(args.artifact_dir / "pages.json", pages)
    partial_path = args.artifact_dir / "extractions_partial.json"
    extractions = read_json(partial_path) if partial_path.exists() else []
    completed = {
        (item.get("requested_url"), bool(item.get("fallback")))
        for item in extractions
    }
    next_allowed = 0.0
    for page in pages:
        key = (page.get("requested_url"), bool(page.get("fallback")))
        if key in completed:
            continue
        now = time.monotonic()
        if next_allowed > now:
            time.sleep(next_allowed - now)
        extractions.append(extract_one(llm, page, EXTRACTION_SCHEMA))
        completed.add(key)
        write_json(partial_path, extractions)
        next_allowed = max(time.monotonic(), next_allowed) + max(0.0, float(args.interval))

    write_json(args.artifact_dir / "extractions.json", extractions)
    dry_run = build_dry_run(
        snapshot,
        pages,
        extractions,
        artifact_dir=args.artifact_dir,
    )
    write_json(args.artifact_dir / "dry_run.json", dry_run)
    print(
        json.dumps(
            {
                "dry_run": str(args.artifact_dir / "dry_run.json"),
                "pages": len(pages),
                "extractions": len(extractions),
                "successful_extractions": sum(1 for item in extractions if not item.get("error")),
                "action_counts": dry_run["action_counts"],
            },
            ensure_ascii=False,
            indent=2,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
