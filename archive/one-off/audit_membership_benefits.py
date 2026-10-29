#!/usr/bin/env python3
"""Deterministic QA for clinic_memberships.benefits (Rule A–G).

Reads output/membership_audit_snapshot.json, writes membership_benefits_audit.csv.
Optionally scrapes source_urls via local Firecrawl and stores raw evidence under
output/membership_scrape/.

Usage:
  python3 one-off/audit_membership_benefits.py
  python3 one-off/audit_membership_benefits.py --scrape --plan-ids 411,242,247
  python3 one-off/audit_membership_benefits.py --scrape --all
"""

from __future__ import annotations

import argparse
import csv
import json
import os
import re
import sys
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path
from typing import Any, Optional

# Bypass local HTTP proxies for LAN Firecrawl
for _k in (
    "http_proxy",
    "https_proxy",
    "HTTP_PROXY",
    "HTTPS_PROXY",
    "ALL_PROXY",
    "all_proxy",
):
    os.environ.pop(_k, None)
os.environ["NO_PROXY"] = "*"
os.environ["no_proxy"] = "*"

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))
from scrape_utils import discover_scrape_path, normalize_url, scrape_one, url_key  # noqa: E402

DEFAULT_SNAPSHOT = ROOT / "output" / "membership_audit_snapshot.json"
DEFAULT_OUT = ROOT / "output" / "membership_benefits_audit.csv"
DEFAULT_SCRAPE_DIR = ROOT / "output" / "membership_scrape"
DEFAULT_BASE = os.environ.get("FIRECRAWL_BASE_URL", "http://192.168.1.68:3002")
DEFAULT_KEY = os.environ.get("FIRECRAWL_API_KEY", "local-monitor-only-20260829")

# P0/P1 from the repair plan §8
P0_P1_PLAN_IDS = [
    411, 242, 247, 355, 368, 452, 519, 669, 672,
    331, 332,
    333, 413, 414, 561,
    304, 443, 444, 445, 396, 397, 401, 402, 409, 410, 422, 423, 497, 498, 499,
    337, 338, 339, 356, 357,
]

# --- Rule patterns ---
SAVINGS_RE = re.compile(
    r"annual\s+savings|save\s+up\s+to|\$[\d,]+\+?\s*savings|value\s+of\s+\$",
    re.I,
)
INHERIT_RE = re.compile(
    r"all\s+.+\s+benefits\s+plus|includes?\s+all\s+.+\s+benefits|"
    r"all\s+above|previous\s+tier|same\s+benefits\s+as|"
    r"receive\s+all\s+above|all\s+.+\s+perks",
    re.I,
)
PROMO_RE = re.compile(
    r"this\s+month|limited\s+time|through\s+\w+\s+\d{1,2}|"
    r"\bpromotion\b|\bpromo\b|special\s+this\s+month",
    re.I,
)
RESTRICT_RE = re.compile(
    r"\bcommitment\b|\bminimum\b|does\s+not\s+include|\bexcluding\b|"
    r"signup\s+fee|cancellation|non[- ]?transferable|\bmust\b|\brequired\b|"
    r"\bcancel\s+with\b|auto\s+draft",
    re.I,
)
MARKETING_RE = re.compile(
    r"\bimproves?\b|\btransforms?\b|\bprevents?\b|maintains?\s+results|"
    r"collagen\s+building|acne\s+management|rosacea\s+control|"
    r"glass\s+skin|cumulative\s+collagen|true\s+glass",
    re.I,
)
# Pure restriction / fee / heading-only (no entitlement)
PURE_RESTRICT_RE = re.compile(
    r"^(\d+-?\s*month\s+commitment|cancel\s+with\s+\d+\s*days?\s+notice|"
    r"full\s+signup\s+fee.*|signup\s+fee.*|payment\s+by\s+auto\s+draft\s+only\.?|"
    r"non[- ]?transferable\.?)$",
    re.I,
)
HEADING_ONLY_RE = re.compile(
    r"^(all\s+.+\s+benefits\s+plus:?|includes?\s+all\s+.+\s+benefits.*|"
    r"receive\s+all\s+above.*|unlock\s+savings\s+for.+)$",
    re.I,
)


def as_benefits(val: Any) -> list[str]:
    if val is None:
        return []
    if isinstance(val, list):
        return [str(x).strip() for x in val if str(x).strip()]
    if isinstance(val, str):
        s = val.strip()
        if not s or s == "[]":
            return []
        try:
            parsed = json.loads(s)
            if isinstance(parsed, list):
                return [str(x).strip() for x in parsed if str(x).strip()]
        except json.JSONDecodeError:
            return [s]
    return []


def benefits_fingerprint(benefits: list[str]) -> str:
    norm = sorted(re.sub(r"\s+", " ", b.lower().strip()) for b in benefits)
    return hashlib.sha1("|".join(norm).encode()).hexdigest()[:16]


def rule_hits(benefits: list[str]) -> list[str]:
    rules: list[str] = []
    if not benefits:
        rules.append("A_EMPTY")
        return rules

    only_savings = all(SAVINGS_RE.search(b) for b in benefits) and any(
        SAVINGS_RE.search(b) for b in benefits
    )
    if only_savings or any(SAVINGS_RE.search(b) for b in benefits):
        # Flag if ANY savings summary present; escalate if ONLY savings
        rules.append("B_SAVINGS_ONLY" if only_savings else "B_SAVINGS_MIXED")

    if any(INHERIT_RE.search(b) or HEADING_ONLY_RE.search(b) for b in benefits):
        rules.append("C_INHERITANCE")

    if any(PROMO_RE.search(b) for b in benefits):
        rules.append("D_PROMO")

    if any(PURE_RESTRICT_RE.search(b) or RESTRICT_RE.search(b) for b in benefits):
        # distinguish pure vs mixed later in notes
        if any(PURE_RESTRICT_RE.search(b) for b in benefits):
            rules.append("E_PURE_RESTRICTION")
        else:
            rules.append("E_RESTRICTION_MIXED")

    if any(MARKETING_RE.search(b) for b in benefits):
        rules.append("F_MARKETING")

    return rules


def severity(rules: list[str]) -> str:
    if not rules:
        return "OK"
    hard = {
        "A_EMPTY",
        "B_SAVINGS_ONLY",
        "C_INHERITANCE",
        "D_PROMO",
        "E_PURE_RESTRICTION",
        "F_MARKETING",
        "G_SUSPICIOUS_DUPLICATE",
    }
    if any(r in hard for r in rules):
        return "FAIL" if "A_EMPTY" in rules or "B_SAVINGS_ONLY" in rules else "REVIEW_REQUIRED"
    return "REVIEW_REQUIRED"


def run_scrape(urls: list[str], scrape_dir: Path, base: str, key: str, concurrency: int) -> dict[str, Any]:
    try:
        import httpx
    except ImportError:
        print("httpx required: pip install httpx", file=sys.stderr)
        raise

    scrape_dir.mkdir(parents=True, exist_ok=True)
    meta_path = scrape_dir / "_index.jsonl"
    # resume
    done_map: dict[str, dict[str, Any]] = {}
    if meta_path.exists():
        with meta_path.open(encoding="utf-8") as f:
            for line in f:
                if not line.strip():
                    continue
                obj = json.loads(line)
                done_map[normalize_url(obj["url"])] = obj

    to_fetch = []
    seen = set()
    for u in urls:
        nu = normalize_url(u)
        if not u or nu in seen:
            continue
        seen.add(nu)
        existing = done_map.get(nu) or {}
        if (existing.get("markdown") or "").strip() or (scrape_dir / f"{url_key(u)}.md").exists():
            continue
        to_fetch.append(u)

    summary = {"requested": len(seen), "to_fetch": len(to_fetch), "ok": 0, "fail": 0}
    if not to_fetch:
        print(json.dumps({"scrape": "nothing_to_fetch", **summary}, indent=2))
        return summary

    with httpx.Client(trust_env=False) as client:
        path = discover_scrape_path(client, base, key)
        print(f"Scraping {len(to_fetch)} URLs via {base}{path}", flush=True)

        def work(u: str) -> dict[str, Any]:
            sr = scrape_one(client, base, path, key, u)
            return {
                "url": u,
                "ok": sr.ok,
                "markdown": sr.markdown,
                "price_text": sr.price_text,
                "container_n": len(sr.containers),
                "error": sr.error,
                "source": "live" if sr.ok else "none",
            }

        with ThreadPoolExecutor(max_workers=max(1, concurrency)) as ex:
            futs = {ex.submit(work, u): u for u in to_fetch}
            with meta_path.open("a", encoding="utf-8") as meta_f:
                for fut in as_completed(futs):
                    res = fut.result()
                    if res["ok"]:
                        summary["ok"] += 1
                        (scrape_dir / f"{url_key(res['url'])}.md").write_text(
                            res["markdown"], encoding="utf-8"
                        )
                    else:
                        summary["fail"] += 1
                    meta_f.write(
                        json.dumps(
                            {
                                "url": res["url"],
                                "ok": res["ok"],
                                "error": res["error"],
                                "source": res["source"],
                                "md_len": len(res.get("markdown") or ""),
                                "markdown": res.get("markdown") or "",
                                "price_text": res.get("price_text") or "",
                                "container_n": res.get("container_n") or 0,
                            },
                            ensure_ascii=False,
                        )
                        + "\n"
                    )
                    meta_f.flush()
    print(json.dumps({"scrape": "done", **summary}, indent=2))
    return summary


def audit_rows(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    # Rule G: same business, different name/price, identical benefits
    by_biz: dict[Any, list[dict[str, Any]]] = {}
    for r in rows:
        by_biz.setdefault(r.get("business_id"), []).append(r)

    dup_plans: set[int] = set()
    for biz, group in by_biz.items():
        buckets: dict[str, list[dict[str, Any]]] = {}
        for r in group:
            bens = as_benefits(r.get("benefits"))
            if not bens:
                continue
            fp = benefits_fingerprint(bens)
            buckets.setdefault(fp, []).append(r)
        for fp, members in buckets.items():
            if len(members) < 2:
                continue
            names = {m.get("membership_name") for m in members}
            prices = {str(m.get("membership_price")) for m in members}
            if len(names) > 1 or len(prices) > 1:
                for m in members:
                    dup_plans.add(int(m["plan_id"]))

    out: list[dict[str, Any]] = []
    for r in rows:
        bens = as_benefits(r.get("benefits"))
        rules = rule_hits(bens)
        if int(r["plan_id"]) in dup_plans:
            rules.append("G_SUSPICIOUS_DUPLICATE")
        notes_parts: list[str] = []
        for b in bens:
            tags = []
            if SAVINGS_RE.search(b):
                tags.append("savings")
            if INHERIT_RE.search(b) or HEADING_ONLY_RE.search(b):
                tags.append("inherit")
            if PROMO_RE.search(b):
                tags.append("promo")
            if PURE_RESTRICT_RE.search(b):
                tags.append("pure_restrict")
            elif RESTRICT_RE.search(b):
                tags.append("restrict")
            if MARKETING_RE.search(b):
                tags.append("marketing")
            if tags:
                notes_parts.append(f"[{','.join(tags)}] {b[:80]}")
        out.append(
            {
                "plan_id": r["plan_id"],
                "business_id": r.get("business_id"),
                "business_name": r.get("business_name") or "",
                "membership_name": r.get("membership_name") or "",
                "membership_price": r.get("membership_price"),
                "billing_period": r.get("billing_period") or "",
                "n_benefits": len(bens),
                "benefits_json": json.dumps(bens, ensure_ascii=False),
                "source_url": r.get("source_url") or "",
                "need_browser": r.get("need_browser"),
                "is_ocr_required": r.get("is_ocr_required"),
                "is_offer_extracted": r.get("is_offer_extracted"),
                "updated_at": r.get("updated_at") or "",
                "rules": "|".join(rules),
                "severity": severity(rules),
                "notes": " || ".join(notes_parts),
                "is_p0_p1": int(r["plan_id"]) in P0_P1_PLAN_IDS,
            }
        )
    return out


def write_csv(path: Path, rows: list[dict[str, Any]]) -> None:
    fields = [
        "plan_id",
        "business_id",
        "business_name",
        "membership_name",
        "membership_price",
        "billing_period",
        "n_benefits",
        "benefits_json",
        "source_url",
        "need_browser",
        "is_ocr_required",
        "is_offer_extracted",
        "updated_at",
        "rules",
        "severity",
        "notes",
        "is_p0_p1",
    ]
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=fields)
        w.writeheader()
        for r in rows:
            w.writerow({k: r.get(k, "") for k in fields})


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--snapshot", type=Path, default=DEFAULT_SNAPSHOT)
    ap.add_argument("--out", type=Path, default=DEFAULT_OUT)
    ap.add_argument("--scrape", action="store_true")
    ap.add_argument("--scrape-dir", type=Path, default=DEFAULT_SCRAPE_DIR)
    ap.add_argument("--base-url", default=DEFAULT_BASE)
    ap.add_argument("--api-key", default=DEFAULT_KEY)
    ap.add_argument("--concurrency", type=int, default=3)
    ap.add_argument("--plan-ids", default="", help="comma-separated plan_ids to scrape")
    ap.add_argument("--all", action="store_true", help="scrape all snapshot source_urls")
    ap.add_argument("--p0", action="store_true", help="scrape P0/P1 plan_ids only")
    args = ap.parse_args()

    data = json.loads(args.snapshot.read_text(encoding="utf-8"))
    rows = data["rows"] if isinstance(data, dict) else data

    audited = audit_rows(rows)
    write_csv(args.out, audited)

    counts: dict[str, int] = {}
    rule_counts: dict[str, int] = {}
    for r in audited:
        counts[r["severity"]] = counts.get(r["severity"], 0) + 1
        for rule in (r["rules"] or "").split("|"):
            if rule:
                rule_counts[rule] = rule_counts.get(rule, 0) + 1

    summary = {
        "total": len(audited),
        "severity": counts,
        "rules": rule_counts,
        "p0_p1_flagged": sum(
            1 for r in audited if r["is_p0_p1"] and r["severity"] != "OK"
        ),
        "out": str(args.out),
    }
    print(json.dumps(summary, indent=2, ensure_ascii=False))

    if args.scrape:
        plan_filter: Optional[set[int]] = None
        if args.plan_ids:
            plan_filter = {int(x) for x in args.plan_ids.split(",") if x.strip()}
        elif args.p0:
            plan_filter = set(P0_P1_PLAN_IDS)
        elif not args.all:
            plan_filter = set(P0_P1_PLAN_IDS)

        urls = []
        for r in rows:
            if plan_filter is not None and int(r["plan_id"]) not in plan_filter:
                continue
            u = (r.get("source_url") or "").strip()
            if u:
                urls.append(u)
        run_scrape(urls, args.scrape_dir, args.base_url, args.api_key, args.concurrency)

    return 0


if __name__ == "__main__":
    sys.exit(main())
