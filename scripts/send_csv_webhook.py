#!/usr/bin/env python3
"""Send CSV rows to a webhook in batches."""
from __future__ import annotations

import argparse
import csv
import json
import os
import sys
import time
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional

import requests

PROJECT_ROOT = Path(__file__).resolve().parent.parent
OUTPUT_DIR = PROJECT_ROOT / "output" / "results"
DEFAULT_WEBHOOK_URL = os.getenv("WEBHOOK_URL") or "https://flows.brandrap.co/webhook/274496d1-5bf1-4d27-a558-2050e9c0e837"


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Send CSV rows to a webhook")
    parser.add_argument("--csv", required=True, help="Input CSV path")
    parser.add_argument("--webhook-url", default=DEFAULT_WEBHOOK_URL, help="Webhook URL")
    parser.add_argument("--batch-size", type=int, default=25, help="Rows per webhook request")
    parser.add_argument("--timeout", type=int, default=240, help="Request timeout in seconds")
    parser.add_argument("--sleep", type=float, default=0.2, help="Delay between requests")
    parser.add_argument("--send", action="store_true", help="Actually POST the rows")
    parser.add_argument("--dry-run", action="store_true", help="Preview only, do not POST")
    parser.add_argument("--quiet", action="store_true", help="Print only summary lines")
    return parser.parse_args()


def read_csv_rows(csv_path: Path) -> List[Dict[str, Any]]:
    with csv_path.open("r", encoding="utf-8-sig", newline="") as handle:
        reader = csv.DictReader(handle)
        rows: List[Dict[str, Any]] = []
        for raw in reader:
            cleaned = {key: (value.strip() if isinstance(value, str) else value) for key, value in raw.items()}
            if not any(str(value or "").strip() for value in cleaned.values()):
                continue
            rows.append(cleaned)
    return rows


def chunked(items: Iterable[Dict[str, Any]], size: int) -> Iterable[List[Dict[str, Any]]]:
    batch: List[Dict[str, Any]] = []
    for item in items:
        batch.append(item)
        if len(batch) >= size:
            yield batch
            batch = []
    if batch:
        yield batch


def post_payload(webhook_url: str, payload: Any, timeout: int) -> Dict[str, Any]:
    response = requests.post(webhook_url, json=payload, timeout=timeout)
    result: Dict[str, Any] = {
        "status_code": response.status_code,
        "ok": response.ok,
        "response_text": response.text[:4000],
    }
    if "application/json" in response.headers.get("content-type", "").lower():
        try:
            result["response_json"] = response.json()
        except ValueError:
            pass
    if not response.ok:
        result["error"] = f"HTTP {response.status_code}"
    return result


def main() -> None:
    args = parse_args()
    csv_path = Path(args.csv).expanduser().resolve()
    if not csv_path.exists():
        raise FileNotFoundError(f"CSV not found: {csv_path}")

    rows = read_csv_rows(csv_path)
    if not rows:
        raise RuntimeError("CSV contains no usable rows")

    batch_size = max(1, int(args.batch_size or 1))
    batches = list(chunked(rows, batch_size))
    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
    report_path = OUTPUT_DIR / f"csv_webhook_send_report_{timestamp}.json"

    report: Dict[str, Any] = {
        "generated_at": timestamp,
        "csv_path": str(csv_path),
        "webhook_url": args.webhook_url,
        "row_count": len(rows),
        "batch_size": batch_size,
        "batch_count": len(batches),
        "send": bool(args.send and not args.dry_run),
        "results": [],
    }

    def write_report() -> None:
        report_path.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")

    for index, batch in enumerate(batches, start=1):
        payload: Any = batch[0] if len(batch) == 1 else batch
        result: Dict[str, Any] = {
            "index": index,
            "batch_size": len(batch),
            "row_count": len(batch),
            "first_row": batch[0],
            "last_row": batch[-1],
        }
        if args.send and not args.dry_run:
            try:
                request_result = post_payload(args.webhook_url, payload, args.timeout)
            except Exception as exc:  # noqa: BLE001
                request_result = {"ok": False, "error": repr(exc), "exception_type": type(exc).__name__}
            result.update(request_result)
            if args.sleep > 0:
                time.sleep(args.sleep)
        report["results"].append(result)
        write_report()
        if not args.quiet:
            print(json.dumps(result, ensure_ascii=False))
        else:
            print(
                json.dumps(
                    {
                        "index": result["index"],
                        "batch_size": result["batch_size"],
                        "ok": result.get("ok"),
                        "status_code": result.get("status_code"),
                        "error": result.get("error"),
                    },
                    ensure_ascii=False,
                )
            )
            sys.stdout.flush()

    ok_count = sum(1 for item in report["results"] if item.get("ok"))
    error_count = sum(1 for item in report["results"] if item.get("ok") is False)
    print(
        json.dumps(
            {
                "report_path": str(report_path),
                "row_count": len(rows),
                "batch_count": len(batches),
                "ok_count": ok_count,
                "error_count": error_count,
                "sent": bool(args.send and not args.dry_run),
            },
            ensure_ascii=False,
        )
    )


if __name__ == "__main__":
    main()
