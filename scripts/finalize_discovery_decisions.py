import argparse
import collections
import csv
import json
import sys
from datetime import date
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

BUCKET = 50


def main() -> int:
    parser = argparse.ArgumentParser(description="Final add/skip decisions once the deep render review has run")
    parser.add_argument("--decisions", type=Path, default=PROJECT_ROOT / "state/discovery/monitor_decisions_final.json")
    parser.add_argument("--deep", type=Path, default=PROJECT_ROOT / "state/discovery/render_review/deep/verdicts.json")
    parser.add_argument("--tiers", type=Path, default=PROJECT_ROOT / "state/discovery/tiers_final.csv")
    parser.add_argument("--out-config", type=Path, default=PROJECT_ROOT / "state/discovery/promo_discovery_monitor_final.json")
    parser.add_argument("--date", default=date.today().strftime("%Y%m%d"))
    args = parser.parse_args()

    payload = json.loads(args.decisions.read_text())
    deep = json.loads(args.deep.read_text())
    for record in payload["decisions"]:
        verdict = deep.get(record["url"])
        if verdict and record.get("status") == "needs_review":
            record["render_review_deep"] = {k: verdict.get(k) for k in ("verdict", "reason", "source", "controls", "clicked", "images_read")}
            if verdict["verdict"] == "verified":
                record["decision"] = "add"
                record.setdefault("reasons", []).append("render_verified_after_expanding")
            else:
                record["decision"] = "skip"
                record.setdefault("reasons", []).append("not_visible_after_expanding_or_unreachable")
        elif record["decision"] == "add_with_review" and record.get("status") == "valid":
            record["decision"] = "add"
            record.setdefault("reasons", []).append("valid_page_concrete_offer")
    remaining = [r["url"] for r in payload["decisions"] if r["decision"] == "add_with_review"]
    assert not remaining, remaining

    counts = collections.Counter(r["decision"] for r in payload["decisions"])
    payload["summary"] = dict(counts)
    args.decisions.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n")

    urls = sorted({r["url"] for r in payload["decisions"] if r["decision"] == "add"})
    monitors = {}
    for number, start in enumerate(range(0, len(urls), BUCKET), start=1):
        key = f"costfinder_discovered_{args.date}_{number:02d}"
        monitors[key] = {"name": key, "status": "active",
                         "targets": [{"id": f"{key}_scrape", "type": "scrape", "urls": urls[start:start + BUCKET]}]}
    args.out_config.write_text(json.dumps({"schema_version": 1, "monitors": monitors}, ensure_ascii=False, indent=2) + "\n")

    pages = collections.Counter(str(r["business_id"]) for r in payload["decisions"] if r["decision"] == "add")
    rows = list(csv.DictReader(args.tiers.open()))
    for row in rows:
        row["pages_found"] = pages.get(row["business_id"], 0)
        row["new_tier"] = "A" if row["current_tier"] == "A" or row["pages_found"] else row["current_tier"]
    with args.tiers.open("w", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=list(rows[0]))
        writer.writeheader()
        writer.writerows(rows)
    print("decisions", dict(counts), "urls", len(urls), "businesses", len(pages))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
