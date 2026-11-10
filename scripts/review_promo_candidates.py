#!/usr/bin/env python3
"""Review a discovery batch and decide which pages are added.

Reads ``<batch-dir>/discovered.json`` and writes ``<batch-dir>/decisions.json``, the file every later stage
reads. Approved pages join the monitor queue once ``extract-pages`` writes them into ``clinic_promotions`` /
``clinic_memberships``. The database is read through an automatic read-only snapshot; nothing is written.
"""
from __future__ import annotations

import argparse
import collections
import json
import sys
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Any, Dict, List
from urllib.parse import urlparse

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from utils.discovery_batch import DISCOVERY_DIR, decisions_path, discovered_path  # noqa: E402
from utils.discovery_snapshot import business_contexts, read_discovery_snapshot  # noqa: E402
from utils.firecrawl_scrape_raw_db import canonical_scrape_url  # noqa: E402
from utils.promo_page_discovery import EXCLUDED_PATH_RE  # noqa: E402
from utils.promo_value_review import decide  # noqa: E402


def load_pages(path: Path) -> List[Dict[str, Any]]:
    pages: List[Dict[str, Any]] = []
    for business in json.loads(path.read_text(encoding="utf-8")).get("businesses", []):
        for page in business.get("pages", []):
            # 早先的发现结果按现行发现规则复核：URL 规范化（路径中的重复斜杠），条款页等排除路径不作为来源
            url = canonical_scrape_url(page["url"])
            if EXCLUDED_PATH_RE.search(urlparse(url).path):
                continue
            pages.append({**page, "url": url, "business_id": business["business_id"]})
    return pages


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--batch-dir", type=Path, default=DISCOVERY_DIR)
    parser.add_argument("--today", help="override today's date (YYYY-MM-DD) for tests")
    args = parser.parse_args()

    today = date.fromisoformat(args.today) if args.today else date.today()
    contexts = business_contexts(read_discovery_snapshot(args.batch_dir))
    decisions = decide(load_pages(discovered_path(args.batch_dir)), contexts, today)
    counts = collections.Counter(d["decision"] for d in decisions)
    reasons = collections.Counter(d["reasons"][-1] for d in decisions if d["decision"] == "skip")
    selected = {d["url"] for d in decisions if d["decision"] in ("add", "add_with_review")}
    summary = {"pages": len(decisions), **counts, "skip_reasons": dict(reasons.most_common(8)),
               "businesses_with_additions": len({d["business_id"] for d in decisions if d["url"] in selected})}
    out = decisions_path(args.batch_dir)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps({"generated_at": datetime.now(timezone.utc).isoformat(), "summary": summary,
                               "decisions": decisions}, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(json.dumps(summary, ensure_ascii=False))
    print(f"wrote {out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
