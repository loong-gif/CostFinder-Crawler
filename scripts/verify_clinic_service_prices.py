#!/usr/bin/env python3
"""Verify clinic_services regular_price against live Firecrawl scrape (+ cache fallback).

Reads exported services CSV, scrapes distinct source_urls once, extracts per-service
regular price + normalized evidence, writes audit CSV. Does NOT update Supabase.

Usage:
  python3 scripts/verify_clinic_service_prices.py \\
    --services-csv output/clinic_services_export.csv \\
    --cache-jsonl output/firecrawl_cache_fallback.jsonl \\
    --out output/clinic_services_price_audit.csv
"""

from __future__ import annotations

import argparse
import csv
import json
import os
import re
import sys
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass, field
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Any, Optional
from urllib.parse import urlsplit

try:
    import httpx
except ImportError:  # pragma: no cover
    print("httpx required: pip install httpx", file=sys.stderr)
    raise

sys.path.insert(0, str(Path(__file__).resolve().parent))
from scrape_utils import (  # noqa: E402
    discover_scrape_path,
    load_cache_jsonl,
    normalize_url,
    scrape_one,
    url_key,
)

# Bypass local HTTP proxies for LAN Firecrawl (common misconfig: http_proxy -> :7890).
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

DEFAULT_BASE = "http://192.168.1.68:3002"
DEFAULT_KEY = os.environ.get("FIRECRAWL_API_KEY", "local-monitor-only-20260829")

# service_name -> alias regexes (first match wins among candidate lines)
SERVICE_ALIASES: dict[str, list[str]] = {
    "Botox": [r"\bbotox\b", r"\bbotulinum\b"],
    "Dysport": [r"\bdysport\b"],
    "Daxxify": [r"\bdaxxify\b"],
    "Xeomin": [r"\bxeomin\b"],
    "Jeuveau": [r"\bjeuveau\b", r"\bnewtox\b"],
    "Letybo": [r"\bletybo\b"],
    "Juvéderm": [r"\bjuv[eé]derm\b", r"\bjuvederm\b"],
    "Sculptra": [r"\bsculptra\b"],
    "Radiesse": [r"\bradiesse\b"],
    "Dermal Filler": [
        r"\bdermal\s*fillers?\b",
        r"\bfillers?\b",
        r"\brestylane\b",
        r"\bversa\b",
        r"\brevanesse\b",
    ],
}

PROMO_RE = re.compile(
    r"\b(member|membership|vip|special|promo|promotion|sale|discount|off\b|"
    r"new\s*client|first\s*time|intro|flash|limited|coupon|deal)\b",
    re.I,
)
REGULAR_HINT_RE = re.compile(
    r"\b(regular|standard|menu|list\s*price|retail|non[- ]?member|"
    r"original\s*price|everyday)\b",
    re.I,
)
FROM_RE = re.compile(r"\bfrom\b", re.I)
UNIT_HINT_RE = re.compile(
    r"\b(per\s+)?(unit|syringe|vial|area|treatment|session|ml|mg)\b", re.I
)
# Require $ or USD — bare integers like "1/2 syringe" must not count as prices.
PRICE_RE = re.compile(
    r"(?:USD\s*)?\$\s*(\d{1,3}(?:,\d{3})*(?:\.\d{1,2})?)\s*"
    r"(?:/\s*|\s+per\s+)?(unit|syringe|vial|area|treatment|session|ml|mg)?",
    re.I,
)
RANGE_RE = re.compile(
    r"\$\s*(\d+(?:\.\d+)?)\s*[-–—to]+\s*\$?\s*(\d+(?:\.\d+)?)", re.I
)
NAV_NOISE_RE = re.compile(
    r"^(skip to|search this|embedded files|cookie|privacy|terms|"
    r"book now|call us|follow us|instagram|facebook)\b",
    re.I,
)


@dataclass
class PriceHit:
    amount: Decimal
    line_idx: int
    line: str
    is_promo: bool
    is_from: bool
    has_unit: bool
    has_regular_hint: bool
    is_range: bool


@dataclass
class ExtractResult:
    proposed_price: Optional[Decimal]
    evidence: str
    verdict_hint: str  # regular | promo_only | not_found
    needs_review: bool
    notes: list[str] = field(default_factory=list)


def parse_decimal(val: Any) -> Optional[Decimal]:
    if val is None or val == "":
        return None
    try:
        return Decimal(str(val).replace(",", "").strip())
    except (InvalidOperation, ValueError):
        return None


def prices_equal(a: Optional[Decimal], b: Optional[Decimal]) -> bool:
    if a is None or b is None:
        return False
    return a.quantize(Decimal("0.01")) == b.quantize(Decimal("0.01"))


def service_alias_res(service_name: str) -> list[re.Pattern[str]]:
    pats = SERVICE_ALIASES.get(service_name, [re.escape(service_name)])
    return [re.compile(p, re.I) for p in pats]


def line_matches_service(line: str, service_name: str) -> bool:
    low = line.lower()
    # Avoid cross-talk: Botox line shouldn't match when looking for Dermal Filler via "filler"
    # if a more specific brand is named on the same line alone — still OK for Dermal Filler.
    for rx in service_alias_res(service_name):
        if rx.search(line):
            # For generic filler aliases, require service_name == Dermal Filler (caller ensures).
            if service_name == "Dermal Filler" and rx.pattern in (r"\bfillers?\b",):
                # Skip lines that are clearly a named brand already covered elsewhere
                if re.search(
                    r"\b(juv[eé]derm|juvederm|sculptra|radiesse|restylane|versa|revanesse)\b",
                    low,
                ):
                    # Still allow if "dermal filler" framing + brand — keep if filler word present
                    if "dermal" not in low and "filler" in low:
                        return True
                    return True
            return True
    return False


def classify_line(line: str) -> tuple[bool, bool, bool]:
    is_promo = bool(PROMO_RE.search(line))
    is_from = bool(FROM_RE.search(line))
    has_regular = bool(REGULAR_HINT_RE.search(line))
    return is_promo, is_from, has_regular


def parse_price_hits_on_line(
    line: str,
    line_idx: int,
    *,
    is_promo: bool,
    is_from: bool,
    has_regular: bool,
) -> list[PriceHit]:
    hits: list[PriceHit] = []
    for m in RANGE_RE.finditer(line):
        a, b = parse_decimal(m.group(1)), parse_decimal(m.group(2))
        if a is None or b is None:
            continue
        hits.append(
            PriceHit(
                amount=a,
                line_idx=line_idx,
                line=line.strip(),
                is_promo=is_promo,
                is_from=True,
                has_unit=bool(UNIT_HINT_RE.search(line)),
                has_regular_hint=has_regular,
                is_range=True,
            )
        )
    if hits:
        return hits
    for m in PRICE_RE.finditer(line):
        amt = parse_decimal(m.group(1))
        if amt is None or amt <= 0:
            continue
        unit = (m.group(2) or "").lower()
        has_unit = bool(unit) or bool(UNIT_HINT_RE.search(line))
        hits.append(
            PriceHit(
                amount=amt,
                line_idx=line_idx,
                line=line.strip(),
                is_promo=is_promo and not has_regular,
                is_from=is_from,
                has_unit=has_unit,
                has_regular_hint=has_regular,
                is_range=False,
            )
        )
    return hits


def extract_hits_from_line(line: str, line_idx: int, service_name: str) -> list[PriceHit]:
    if not line_matches_service(line, service_name):
        return []
    is_promo, is_from, has_regular = classify_line(line)
    return parse_price_hits_on_line(
        line, line_idx, is_promo=is_promo, is_from=is_from, has_regular=has_regular
    )


def page_implies_service(url: str, lines: list[str], service_name: str) -> bool:
    """True when URL path or top heading clearly centers this service."""
    path = urlsplit(url).path.lower()
    for rx in service_alias_res(service_name):
        # strip regex noise for crude path check
        token = re.sub(r"[\\^$?*+\[\](){}.|]", "", rx.pattern)
        token = token.replace("b", "").strip()
        # use first literal-ish alias word
    alias_words: list[str] = []
    for raw in SERVICE_ALIASES.get(service_name, [service_name]):
        # pick simple word tokens from alias patterns
        alias_words.extend(re.findall(r"[a-zA-Z]{4,}", raw))
    alias_words.append(service_name.lower().replace("é", "e"))
    for w in alias_words:
        w = w.lower()
        if w and w in path.replace("-", "").replace("_", ""):
            return True
        if w and re.search(rf"/{re.escape(w)}\b", path):
            return True
    head = "\n".join(lines[:25])
    return line_matches_service(head, service_name) and not any(
        line_matches_service(head, other)
        for other in SERVICE_ALIASES
        if other != service_name and other not in ("Dermal Filler",)
        and service_name not in ("Dermal Filler",)
    )


def score_hit(h: PriceHit) -> tuple:
    """Higher is better for regular-price selection."""
    return (
        0 if h.is_promo else 1,
        1 if h.has_regular_hint else 0,
        0 if h.is_from else 1,
        1 if h.has_unit else 0,
        0 if h.is_range else 1,
        # prefer typical tox unit prices over huge package totals when unit present
        float(h.amount) if h.amount < 200 else -float(h.amount),
    )


def normalize_evidence(
    lines: list[str], center_idxs: list[int], service_name: str = ""
) -> str:
    """Pick 1–3 original lines around hits; target ~40–220 chars."""
    if not lines or not center_idxs:
        return ""
    chosen: list[str] = []
    seen = set()

    def add_idx(j: int) -> None:
        if j < 0 or j >= len(lines) or j in seen:
            return
        s = lines[j].strip()
        if not s or NAV_NOISE_RE.search(s):
            return
        seen.add(j)
        chosen.append(s)

    for idx in center_idxs[:3]:
        # Prefer the price line itself first, then neighbors with $ or service name
        ordered = [idx]
        for delta in (-1, 1, -2, 2, -3, 3):
            ordered.append(idx + delta)
        # rank: has $, mentions service, then others
        scored: list[tuple[int, int]] = []
        for j in ordered:
            if j < 0 or j >= len(lines) or j in seen:
                continue
            s = lines[j].strip()
            if not s or NAV_NOISE_RE.search(s):
                continue
            score = 0
            if "$" in s:
                score += 2
            if service_name and line_matches_service(s, service_name):
                score += 1
            # penalize very long marketing blurbs without $
            if "$" not in s and len(s) > 120:
                score -= 2
            scored.append((score, j))
        for score, j in sorted(scored, key=lambda t: (-t[0], abs(t[1] - idx))):
            add_idx(j)
            if len(chosen) >= 3:
                break
        if len(chosen) >= 3:
            break

    # Ensure at least one line mentions the service when possible
    if service_name and chosen and not any(line_matches_service(c, service_name) for c in chosen):
        for idx in center_idxs:
            for j in range(max(0, idx - 4), min(len(lines), idx + 5)):
                if j in seen:
                    continue
                s = lines[j].strip()
                if s and line_matches_service(s, service_name) and not NAV_NOISE_RE.search(s):
                    chosen.append(s)
                    seen.add(j)
                    break
            if any(line_matches_service(c, service_name) for c in chosen):
                break

    def clip_line(s: str) -> str:
        """Keep original characters; if long, window around first $ price."""
        if len(s) <= 160:
            return s
        m = re.search(r"\$\s*\d", s)
        if not m:
            return s[:160]
        start = max(0, m.start() - 40)
        end = min(len(s), m.start() + 120)
        chunk = s[start:end].strip()
        if start > 0:
            chunk = "…" + chunk
        if end < len(s):
            chunk = chunk + "…"
        return chunk

    clipped = [clip_line(c) for c in chosen]
    text = "\n".join(clipped)
    if len(text) > 220:
        acc: list[str] = []
        n = 0
        for s in clipped:
            if n + len(s) + (1 if acc else 0) > 220:
                break
            acc.append(s)
            n += len(s) + (1 if len(acc) > 1 else 0)
        text = "\n".join(acc) if acc else text[:220]
    if len(text.strip()) < 10:
        return ""
    return text.strip()


def extract_for_service(
    markdown: str, service_name: str, source_url: str = ""
) -> ExtractResult:
    if not markdown or not markdown.strip():
        return ExtractResult(None, "", "not_found", False, ["empty markdown"])
    raw_lines = markdown.splitlines()
    lines = [re.sub(r"[ \t]+", " ", ln).strip() for ln in raw_lines]
    all_hits: list[PriceHit] = []
    for i, line in enumerate(lines):
        if not line or NAV_NOISE_RE.search(line):
            continue
        all_hits.extend(extract_hits_from_line(line, i, service_name))

    # Nearby context: price on a line, service name within ±3 lines
    if not all_hits:
        for i, line in enumerate(lines):
            if not line or "$" not in line:
                continue
            ctx = " ".join(
                lines[j] for j in range(max(0, i - 3), min(len(lines), i + 4)) if lines[j]
            )
            if not line_matches_service(ctx, service_name):
                continue
            is_promo, is_from, has_regular = classify_line(ctx)
            nearby = parse_price_hits_on_line(
                line, i, is_promo=is_promo, is_from=is_from, has_regular=has_regular
            )
            # Prefer unit-linked prices in neighborhood mode
            nearby = [h for h in nearby if h.has_unit or h.amount < 100 or "syringe" in line.lower() or "vial" in line.lower()]
            all_hits.extend(nearby)

    # Page-scoped: /botox URL or Botox H1 + "$14/unit" lines without other brands on the price line
    if not all_hits and source_url and page_implies_service(source_url, lines, service_name):
        for i, line in enumerate(lines):
            if "$" not in line:
                continue
            # skip if price line names a different specific toxin/filler brand
            other_named = False
            for other in SERVICE_ALIASES:
                if other == service_name:
                    continue
                if line_matches_service(line, other) and not line_matches_service(line, service_name):
                    other_named = True
                    break
            if other_named:
                continue
            is_promo, is_from, has_regular = classify_line(line)
            hits = parse_price_hits_on_line(
                line, i, is_promo=is_promo, is_from=is_from, has_regular=has_regular
            )
            # On a single-service page, require unit/syringe/vial cue for toxins/fillers
            for h in hits:
                if h.has_unit or UNIT_HINT_RE.search(
                    " ".join(lines[max(0, i - 1) : i + 2])
                ):
                    all_hits.append(h)

    if not all_hits:
        return ExtractResult(None, "", "not_found", False, [])

    regular = [h for h in all_hits if not h.is_promo]
    promo = [h for h in all_hits if h.is_promo]

    notes: list[str] = []
    needs_review = False

    if not regular and promo:
        best = max(promo, key=score_hit)
        ev = normalize_evidence(lines, [best.line_idx], service_name)
        return ExtractResult(best.amount, ev, "promo_only", True, ["only promo/member prices"])

    pool = regular if regular else all_hits
    amounts = {h.amount.quantize(Decimal("0.01")) for h in pool}
    if len(amounts) > 1:
        needs_review = True
        notes.append(f"multiple prices: {sorted(amounts)}")
    if any(h.is_range for h in pool):
        needs_review = True
        notes.append("price range on page")
    if any(h.is_from for h in pool):
        needs_review = True
        notes.append("from-price")

    best = max(pool, key=score_hit)
    # Collect up to 3 distinct evidence lines for this price/service
    idxs = []
    for h in sorted(pool, key=score_hit, reverse=True):
        if h.line_idx not in idxs:
            idxs.append(h.line_idx)
        if len(idxs) >= 3:
            break
    ev = normalize_evidence(lines, idxs, service_name)
    if not ev:
        needs_review = True
        notes.append("weak evidence")
    return ExtractResult(best.amount, ev, "regular", needs_review, notes)


def decide_verdict(
    current: Optional[Decimal],
    extracted: ExtractResult,
    scrape_source: str,
) -> tuple[str, Optional[Decimal], bool, str]:
    if scrape_source == "none":
        return "scrape_failed", None, True, "live+cache failed"

    if extracted.verdict_hint == "not_found":
        return "not_found", None, extracted.needs_review, "; ".join(extracted.notes)

    if extracted.verdict_hint == "promo_only":
        return (
            "promo_only",
            extracted.proposed_price,
            True,
            "; ".join(extracted.notes),
        )

    proposed = extracted.proposed_price
    if current is None:
        return "extracted", proposed, extracted.needs_review, "; ".join(extracted.notes)
    if prices_equal(current, proposed):
        return "verified", proposed, extracted.needs_review, "; ".join(extracted.notes)
    return (
        "price_mismatch",
        proposed,
        extracted.needs_review,
        "; ".join(extracted.notes + [f"db={current} page={proposed}"]),
    )


def load_services(path: Path) -> list[dict[str, str]]:
    with path.open(newline="", encoding="utf-8") as f:
        return list(csv.DictReader(f))


def write_audit(path: Path, rows: list[dict[str, Any]]) -> None:
    fields = [
        "service_id",
        "business_id",
        "service_name",
        "unit_type",
        "source_url",
        "current_price",
        "proposed_price",
        "verdict",
        "needs_review",
        "evidence_normalized",
        "scrape_source",
        "notes",
    ]
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=fields)
        w.writeheader()
        for r in rows:
            w.writerow({k: r.get(k, "") for k in fields})


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--services-csv", type=Path, required=True)
    ap.add_argument("--out", type=Path, default=Path("output/clinic_services_price_audit.csv"))
    ap.add_argument("--cache-jsonl", type=Path, default=None)
    ap.add_argument("--scrape-dir", type=Path, default=Path("output/scrape_live_v2"))
    ap.add_argument("--base-url", default=DEFAULT_BASE)
    ap.add_argument("--api-key", default=DEFAULT_KEY)
    ap.add_argument("--concurrency", type=int, default=4)
    ap.add_argument(
        "--scrape-path",
        default="/v2/scrape",
        help="Firecrawl scrape path (default /v2/scrape with onlyCleanContent)",
    )
    ap.add_argument("--skip-scrape", action="store_true")
    ap.add_argument("--limit-urls", type=int, default=0, help="debug: scrape only N urls")
    ap.add_argument("--smoke-url", default="", help="if set, scrape one URL and exit")
    args = ap.parse_args()

    # Smoke-only mode
    if args.smoke_url:
        with httpx.Client(trust_env=False) as client:
            path = args.scrape_path or discover_scrape_path(client, args.base_url, args.api_key)
            res = scrape_one(client, args.base_url, path, args.api_key, args.smoke_url)
            print(
                json.dumps(
                    {
                        "path": path,
                        "ok": res.ok,
                        "md_len": len(res.markdown),
                        "price_text_len": len(res.price_text),
                        "containers": len(res.containers),
                        "err": res.error,
                        "preview": res.price_text[:300],
                    },
                    ensure_ascii=False,
                )
            )
            return 0 if res.ok else 1

    services = load_services(args.services_csv)
    cache = load_cache_jsonl(args.cache_jsonl)
    args.scrape_dir.mkdir(parents=True, exist_ok=True)

    # Distinct URLs
    urls: list[str] = []
    seen = set()
    for s in services:
        u = (s.get("source_url") or "").strip()
        if not u:
            continue
        nu = normalize_url(u)
        if nu in seen:
            continue
        seen.add(nu)
        urls.append(u)
    if args.limit_urls > 0:
        urls = urls[: args.limit_urls]

    scrape_map: dict[str, dict[str, Any]] = {}  # normalize_url -> {markdown, source, error}
    # Resume from prior live scrapes
    meta_path = args.scrape_dir / "_index.jsonl"
    if meta_path.exists():
        with meta_path.open(encoding="utf-8") as f:
            for line in f:
                if not line.strip():
                    continue
                obj = json.loads(line)
                scrape_map[normalize_url(obj["url"])] = obj

    to_fetch = [u for u in urls if normalize_url(u) not in scrape_map or not scrape_map[normalize_url(u)].get("markdown")]
    live_ok = live_fail = 0

    if not args.skip_scrape and to_fetch:
        print(f"Scraping {len(to_fetch)}/{len(urls)} URLs via {args.base_url} concurrency={args.concurrency}", flush=True)
        with httpx.Client(trust_env=False) as client:
            path = args.scrape_path or discover_scrape_path(client, args.base_url, args.api_key)
            print(f"Using scrape path {path} (onlyCleanContent={'yes' if path.startswith('/v2') else 'n/a'})", flush=True)

            def work(u: str) -> dict[str, Any]:
                sr = scrape_one(client, args.base_url, path, args.api_key, u)
                return {
                    "url": u,
                    "ok": sr.ok,
                    "markdown": sr.markdown,
                    "price_text": sr.price_text,
                    "containers": len(sr.containers),
                    "error": sr.error,
                    "source": "live" if sr.ok else "none",
                }

            with ThreadPoolExecutor(max_workers=max(1, args.concurrency)) as ex:
                futs = {ex.submit(work, u): u for u in to_fetch}
                done = 0
                with meta_path.open("a", encoding="utf-8") as meta_f:
                    for fut in as_completed(futs):
                        res = fut.result()
                        nu = normalize_url(res["url"])
                        scrape_map[nu] = res
                        # persist markdown file
                        if res["ok"]:
                            live_ok += 1
                            (args.scrape_dir / f"{url_key(res['url'])}.md").write_text(res["markdown"], encoding="utf-8")
                        else:
                            live_fail += 1
                        # store compact index line without full md if huge — keep md in map for this run
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
                                    "containers": res.get("containers") or 0,
                                },
                                ensure_ascii=False,
                            )
                            + "\n"
                        )
                        meta_f.flush()
                        done += 1
                        if done % 25 == 0 or done == len(to_fetch):
                            print(f"  progress {done}/{len(to_fetch)} ok={live_ok} fail={live_fail}", flush=True)
    else:
        # count from map
        for u in urls:
            obj = scrape_map.get(normalize_url(u)) or {}
            if obj.get("markdown"):
                live_ok += 1
            else:
                live_fail += 1

    # Resolve markdown per URL: live → cache
    resolved: dict[str, tuple[str, str, str]] = {}  # nu -> (md, price_text, scrape_source)
    cache_hits = 0
    for u in urls:
        nu = normalize_url(u)
        obj = scrape_map.get(nu) or {}
        md = obj.get("markdown") or ""
        if md.strip():
            resolved[nu] = (md, obj.get("price_text") or md, "live")
            continue
        # try loading from .md file
        md_path = args.scrape_dir / f"{url_key(u)}.md"
        if md_path.exists():
            md = md_path.read_text(encoding="utf-8")
            if md.strip():
                resolved[nu] = (md, obj.get("price_text") or md, "live")
                continue
        cmd = cache.get(nu) or cache.get(u) or ""
        if cmd.strip():
            resolved[nu] = (cmd, cmd, "cache")
            cache_hits += 1
        else:
            resolved[nu] = ("", "", "none")

    # Extract per service
    audit_rows: list[dict[str, Any]] = []
    verdict_counts: dict[str, int] = {}
    for s in services:
        u = (s.get("source_url") or "").strip()
        if not u:
            continue
        nu = normalize_url(u)
        md, price_text, scrape_source = resolved.get(nu, ("", "", "none"))
        current = parse_decimal(s.get("regular_price"))
        name = s.get("service_name") or ""
        # price_text (hidden-filtered top-level price divs) is primary evidence;
        # markdown remains the backup when no price containers were found.
        text_for_extraction = price_text or md
        extracted = (
            extract_for_service(text_for_extraction, name, u)
            if text_for_extraction
            else ExtractResult(None, "", "not_found", False, [])
        )
        verdict, proposed, needs_review, notes = decide_verdict(current, extracted, scrape_source)
        if extracted.evidence and verdict != "scrape_failed":
            # keep evidence only when we have page text
            evidence = extracted.evidence
        else:
            evidence = ""
        if scrape_source == "none":
            notes = (notes + "; " if notes else "") + (scrape_map.get(nu) or {}).get("error", "no markdown")
        row = {
            "service_id": s.get("service_id"),
            "business_id": s.get("business_id"),
            "service_name": name,
            "unit_type": s.get("unit_type"),
            "source_url": u,
            "current_price": str(current) if current is not None else "",
            "proposed_price": str(proposed) if proposed is not None else "",
            "verdict": verdict,
            "needs_review": "true" if needs_review else "false",
            "evidence_normalized": evidence,
            "scrape_source": scrape_source,
            "notes": notes,
        }
        audit_rows.append(row)
        verdict_counts[verdict] = verdict_counts.get(verdict, 0) + 1

    write_audit(args.out, audit_rows)

    summary = {
        "services": len(audit_rows),
        "distinct_urls": len(urls),
        "live_ok": live_ok,
        "live_fail": live_fail,
        "cache_fallback_used": cache_hits,
        "unresolved_urls": sum(1 for u in urls if resolved[normalize_url(u)][2] == "none"),
        "verdicts": verdict_counts,
        "out": str(args.out),
    }
    print(json.dumps(summary, indent=2, ensure_ascii=False))
    # Also write failed URL list for cache backfill
    failed_urls = [u for u in urls if resolved[normalize_url(u)][2] == "none"]
    fail_path = args.out.with_suffix(".failed_urls.txt")
    fail_path.write_text("\n".join(failed_urls) + ("\n" if failed_urls else ""), encoding="utf-8")
    # URLs that need cache (live failed but maybe cache exists remotely)
    need_cache = [
        u
        for u in urls
        if not ((scrape_map.get(normalize_url(u)) or {}).get("markdown") or "").strip()
        and normalize_url(u) not in {normalize_url(x) for x in []}
    ]
    # live-failed regardless of cache
    live_failed_urls = []
    for u in urls:
        obj = scrape_map.get(normalize_url(u)) or {}
        if not (obj.get("markdown") or "").strip():
            md_path = args.scrape_dir / f"{url_key(u)}.md"
            if not md_path.exists():
                live_failed_urls.append(u)
    (args.out.with_suffix(".live_failed_urls.txt")).write_text(
        "\n".join(live_failed_urls) + ("\n" if live_failed_urls else ""), encoding="utf-8"
    )
    print(f"live_failed_urls={len(live_failed_urls)} unresolved={len(failed_urls)}", flush=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())
