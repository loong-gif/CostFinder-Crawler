#!/usr/bin/env python3
"""Plan and apply source-backed conditions without an external model.

The planner matches each Offer to its stored source and extracts only explicit
restrictions with small, conservative rules. Ambiguous matches stay in review.
"""
from __future__ import annotations

import argparse
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import sys

from dotenv import load_dotenv

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from utils.offer_conditions_backfill_apply import apply_plan
from utils.offer_conditions_backfill_plan import build_plan
from utils.supabase_rest import SupabaseRestClient, get_supabase_secret_key
from utils.supabase_access import load_supabase_client


def _load_reader() -> SupabaseRestClient:
    url = (os.getenv("SUPABASE_URL") or "").strip()
    key = os.getenv("SUPABASE_PUBLISHABLE_KEY") or os.getenv("SUPABASE_ANON_KEY") or get_supabase_secret_key()
    if not url or not key:
        raise RuntimeError("Missing SUPABASE_URL or a Supabase read credential")
    return SupabaseRestClient(url, key)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--apply", type=Path, help="Apply an existing plan JSON")
    parser.add_argument("--output", type=Path, help="Output JSON path")
    args = parser.parse_args()
    load_dotenv(PROJECT_ROOT / ".env")
    if args.apply:
        plan = json.loads(args.apply.read_text(encoding="utf-8"))
        reader = _load_reader()
        writer = load_supabase_client(PROJECT_ROOT, role="writer")
        result = apply_plan(plan, reader, writer)
        output = args.output or args.apply.parent / (
            f"promo_offer_conditions_apply_{datetime.now(timezone.utc).strftime('%Y%m%d_%H%M%S')}.json"
        )
    else:
        plan = build_plan(_load_reader())
        result = plan
        output = args.output or PROJECT_ROOT / "output" / "results" / (
            f"promo_offer_conditions_plan_{datetime.now(timezone.utc).strftime('%Y%m%d_%H%M%S')}.json"
        )
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps({"output": str(output), **result.get("summary", result)}, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
