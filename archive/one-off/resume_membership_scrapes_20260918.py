#!/usr/bin/env python3
"""Resume only failed membership page scrapes; never writes database rows."""
from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path

from dotenv import load_dotenv

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from scripts.repair_clinic_memberships_full_table import FirecrawlHTTP, scrape_one  # noqa: E402


def read_json(path: Path):
    return json.loads(path.read_text(encoding="utf-8"))


def write_json(path: Path, payload) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    temporary.replace(path)


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--source-artifact-dir", type=Path, required=True)
    parser.add_argument("--artifact-dir", type=Path, required=True)
    parser.add_argument("--interval", type=float, default=1.5)
    args = parser.parse_args()

    load_dotenv(ROOT / ".env")
    source_pages = read_json(args.source_artifact_dir / "firecrawl_main_scrapes.json")
    partial_path = args.artifact_dir / "firecrawl_main_scrapes_partial.json"
    pages = read_json(partial_path) if partial_path.exists() else source_pages
    by_url = {str(page.get("requested_url")): page for page in pages if page.get("requested_url")}
    missing_urls = [
        str(page["requested_url"])
        for page in source_pages
        if not by_url.get(str(page.get("requested_url")), {}).get("markdown")
    ]
    api_key = os.getenv("FIRECRAWL_API_KEY", "").strip()
    if not api_key:
        raise RuntimeError("FIRECRAWL_API_KEY is required")
    fc = FirecrawlHTTP(
        os.getenv("COSTFINDER_FIRECRAWL_URL", "http://192.168.1.68:3002"),
        api_key,
        request_interval=args.interval,
    )
    args.artifact_dir.mkdir(parents=True, exist_ok=True)
    for index, url in enumerate(missing_urls, start=1):
        page = scrape_one(fc, url)
        by_url[url] = page
        write_json(partial_path, list(by_url.values()))
        print(
            json.dumps(
                {
                    "completed": index,
                    "total": len(missing_urls),
                    "url": url,
                    "ok": bool(page.get("markdown")),
                    "error": page.get("error"),
                },
                ensure_ascii=False,
            ),
            flush=True,
        )
    output = args.artifact_dir / "firecrawl_main_scrapes_resumed.json"
    write_json(output, list(by_url.values()))
    print(
        json.dumps(
            {
                "output": str(output),
                "total": len(by_url),
                "with_markdown": sum(bool(page.get("markdown")) for page in by_url.values()),
                "errors": sum(bool(page.get("error")) for page in by_url.values()),
            },
            ensure_ascii=False,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
