#!/usr/bin/env python3
"""Dry-run NakedMD product variant capture; write reconciliation summary JSON."""
from __future__ import annotations

import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

OUT = ROOT / "output/nakedmd_variant_diag_20261020"
URLS = [
    "https://www.nakedmd.com/collections/specials",
]


def main() -> None:
    OUT.mkdir(parents=True, exist_ok=True)
    summary = {"urls": [], "note": "Non-member tiers should map to member regular_price, not separate offers."}
    try:
        from utils.monitor_rendered_browser import render_page
    except Exception as exc:
        summary["error"] = f"browser_unavailable:{type(exc).__name__}"
        (OUT / "summary.json").write_text(json.dumps(summary, indent=2))
        print(json.dumps(summary, indent=2))
        return
    for url in URLS:
        directory = OUT / url.split("/")[-1]
        try:
            browser = render_page(url, directory, business_id=2986)
        except Exception as exc:
            summary["urls"].append({"url": url, "error": type(exc).__name__})
            continue
        entry = {
            "url": url,
            "variant_expected": browser.get("variant_expected"),
            "variant_skipped": browser.get("variant_skipped"),
            "variant_capture_count": len(browser.get("variant_captures") or []),
            "product_variant_reconciliation": browser.get("product_variant_reconciliation"),
            "variants_complete": browser.get("variants_complete"),
        }
        summary["urls"].append(entry)
    (OUT / "summary.json").write_text(json.dumps(summary, indent=2))
    print(json.dumps(summary, indent=2))


if __name__ == "__main__":
    main()
