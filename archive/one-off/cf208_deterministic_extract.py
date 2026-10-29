#!/usr/bin/env python3
"""Deterministic promotions/memberships extraction from scraped markdown (no LLM)."""
from __future__ import annotations
import json, os, re, sys
from pathlib import Path
from datetime import datetime, timezone

ROOT = Path(__file__).resolve().parents[1]
OUT = ROOT / "one-off/outputs/candidates_20261028_208"
OFFERS_CONTRACT = "source_item_name_v1"

LINK_RE = re.compile(r"\[([^\]]*)\]\([^)]+\)")
HTML_RE = re.compile(r"<[^>]+>")
WS_RE = re.compile(r"[ \t]+")
HEADING_RE = re.compile(r"^(#{1,6})\s+(.+)$")
PRICE_RE = re.compile(
    r"(?<!\w)(?:US\s*)?\$(?:\s*)\d{1,3}(?:,\d{3})*(?:\.\d{1,2})?"
    r"|\b\d{1,3}(?:,\d{3})*(?:\.\d{1,2})?\s*(?:USD|usd)\b"
    r"|\b\d{1,2}\s*%\s*off\b",
    re.I,
)
MONEY_NUM_RE = re.compile(r"\$\s*(\d{1,3}(?:,\d{3})*(?:\.\d{1,2})?)")
PROMO_KW = re.compile(
    r"\b(specials?|promo(?:tion)?s?|offer|deal|sale|discount|save|% off|"
    r"limited[- ]time|flash sale|package|bundle|buy\s*\d|bogo|complimentary|"
    r"free consult|new patient|introductory)\b",
    re.I,
)
MEMB_KW = re.compile(
    r"\b(membership|member\s*plan|members?\s*club|loyalty\s*club|"
    r"subscribe|subscription|monthly\s*plan|annual\s*plan|vip\s*club)\b",
    re.I,
)
BILLING_RE = [
    (re.compile(r"\b(per\s*month|/mo\b|monthly)\b", re.I), "monthly"),
    (re.compile(r"\b(per\s*quarter|quarterly|/qtr)\b", re.I), "quarterly"),
    (re.compile(r"\b(per\s*year|yearly|annual|/yr)\b", re.I), "annual"),
    (re.compile(r"\b(one[-\s]?time|single\s*payment)\b", re.I), "one_time"),
]
DATE_RE = re.compile(
    r"\b((?:Jan(?:uary)?|Feb(?:ruary)?|Mar(?:ch)?|Apr(?:il)?|May|Jun(?:e)?|"
    r"Jul(?:y)?|Aug(?:ust)?|Sep(?:t(?:ember)?)?|Oct(?:ober)?|Nov(?:ember)?|"
    r"Dec(?:ember)?)\s+\d{1,2}(?:\s*,\s*\d{4})?"
    r"|\d{4}-\d{2}-\d{2}"
    r"|\d{1,2}/\d{1,2}/\d{2,4})\b",
    re.I,
)
NOISE_LINE = re.compile(
    r"^(subtotal|total|cart|0\s*\$0|\$0\.00|menu|home|skip to|cookie|privacy|"
    r"sign in|log in|search|qty|quantity)\b",
    re.I,
)


def now():
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def clean_md(md: str) -> str:
    text = LINK_RE.sub(r"\1", md)
    text = HTML_RE.sub(" ", text)
    text = text.replace("\u00a0", " ")
    text = re.sub(r"[*_`]+", "", text)
    return text


def lines_of(md: str) -> list[str]:
    out = []
    for raw in clean_md(md).splitlines():
        ln = WS_RE.sub(" ", raw).strip()
        ln = ln.strip("*->• \t")
        if not ln:
            continue
        if NOISE_LINE.search(ln):
            continue
        if len(ln) < 2:
            continue
        out.append(ln)
    return out


def is_heading(ln: str) -> bool:
    if HEADING_RE.match(ln):
        return True
    if ln.isupper() and 3 <= len(ln) <= 80 and not PRICE_RE.search(ln):
        return True
    return False


def heading_text(ln: str) -> str:
    m = HEADING_RE.match(ln)
    return (m.group(2) if m else ln).strip("# ").strip()


def parse_promotions(md: str, source_url: str) -> dict:
    ls = lines_of(md)
    promotions = []
    # Block strategy: heading/promo-kw line starts a block; collect following content
    i = 0
    seen_titles = set()
    while i < len(ls):
        ln = ls[i]
        start = False
        title = None
        if is_heading(ln) and (PROMO_KW.search(ln) or PRICE_RE.search(ln) or (i + 1 < len(ls) and PRICE_RE.search(ls[i + 1]))):
            start = True
            title = heading_text(ln)[:200]
        elif PROMO_KW.search(ln) and PRICE_RE.search(ln):
            start = True
            title = re.split(r"\s+[–—:-]\s+", ln)[0][:200]
            if len(title) < 3:
                title = ln[:120]
        elif PRICE_RE.search(ln) and PROMO_KW.search(" ".join(ls[max(0, i - 2) : i + 1])):
            # price line under recent promo context
            start = True
            # look back for title
            title = None
            for j in range(i - 1, max(-1, i - 6), -1):
                if is_heading(ls[j]) or PROMO_KW.search(ls[j]):
                    title = heading_text(ls[j])[:200]
                    break
            if not title:
                title = ln[:120]
        if not start:
            i += 1
            continue
        content = []
        # include title line if it has substance beyond title
        if PRICE_RE.search(ln) or len(ln) > len(title or ""):
            content.append(ln)
        j = i + 1
        while j < len(ls) and j < i + 12:
            nxt = ls[j]
            if is_heading(nxt) and j > i + 1:
                break
            if NOISE_LINE.search(nxt):
                j += 1
                continue
            content.append(nxt)
            # stop after we have price evidence and a couple supporting lines
            if PRICE_RE.search("\n".join(content)) and len(content) >= 3 and j >= i + 3:
                # peek: if next is also price-ish keep one more
                if j + 1 < len(ls) and PRICE_RE.search(ls[j + 1]) and len(content) < 6:
                    j += 1
                    continue
                j += 1
                break
            j += 1
        # require price or strong promo keyword in content
        blob = "\n".join(content)
        if not content or (not PRICE_RE.search(blob) and not PROMO_KW.search(blob)):
            i += 1
            continue
        title = (title or content[0])[:200].strip()
        key = title.lower()
        if key in seen_titles:
            i = max(i + 1, j)
            continue
        # skip pure nav
        if len(title) < 3:
            i += 1
            continue
        seen_titles.add(key)
        # dates null unless clearly present as range (leave null per schema guidance)
        promotions.append({
            "promotion_title": title,
            "promotion_content": content[:20],
            "campaign_start_date": None,
            "campaign_end_date": None,
        })
        i = max(i + 1, j)

    # Fallback: if page looks promo-ish but no blocks, emit one page-level promo from price lines
    if not promotions:
        price_lines = [ln for ln in ls if PRICE_RE.search(ln) and not re.search(r"^\$0", ln)]
        promo_lines = [ln for ln in ls if PROMO_KW.search(ln)]
        if price_lines and (promo_lines or "special" in source_url.lower() or "promo" in source_url.lower() or "offer" in source_url.lower() or "deal" in source_url.lower()):
            title = promo_lines[0][:200] if promo_lines else (Path(source_url.rstrip("/")).name.replace("-", " ").title() or "Specials")
            content = (promo_lines[:3] + price_lines[:8])
            # dedupe preserve order
            seen = set(); content2 = []
            for c in content:
                if c not in seen:
                    seen.add(c); content2.append(c)
            promotions.append({
                "promotion_title": title[:200],
                "promotion_content": content2[:20],
                "campaign_start_date": None,
                "campaign_end_date": None,
            })

    expl = f"deterministic_md_v1: found {len(promotions)} promotion block(s) from markdown heuristics."
    return {"explanation": expl, "promotions": promotions}



def normalize_simple(value: str) -> str:
    return re.sub(r"\s+", " ", str(value or "").lower()).strip()


def parse_memberships(md: str) -> dict:
    ls = lines_of(md)
    memberships = []
    seen = set()
    junk_name = re.compile(
        r".{90,}|"
        r"^(auto-renewal|annual commitment|\d+\\.|transfers of|unpaid balances|"
        r"membership freeze|what is it\?)",
        re.I,
    )
    benefit_hint = re.compile(
        r"%|\boff\b|include|credit|discount|priority|free |complimentary|save|"
        r"unlimited|access|perk|benefit|can be used",
        re.I,
    )
    for i, ln in enumerate(ls):
        if not MEMB_KW.search(ln) and not (i > 0 and MEMB_KW.search(ls[i - 1]) and PRICE_RE.search(ln)):
            if not (re.search(r"\bjoin\b", ln, re.I) and PRICE_RE.search(ln) and re.search(r"/mo|per month|monthly|annual|/yr", ln, re.I)):
                continue
        window = ls[max(0, i - 2) : i + 6]
        blob = "\n".join(window)
        amounts = []
        for m in MONEY_NUM_RE.finditer(blob):
            try:
                val = float(m.group(1).replace(",", ""))
            except ValueError:
                continue
            if val > 0:
                amounts.append(val)
        if not amounts:
            continue
        if re.search(r"\b(free\s+to\s+join|rewards?\s+program|points?\s+program)\b", blob, re.I) and not re.search(r"/mo|per month|membership\s*fee", blob, re.I):
            continue
        price = amounts[0]
        for m in MONEY_NUM_RE.finditer(blob):
            val = float(m.group(1).replace(",", ""))
            if val <= 0:
                continue
            after = blob[m.end() : m.end() + 20]
            if re.search(r"(/mo|per month|monthly|/yr|per year|annual)", after, re.I):
                price = val
                break
        billing = None
        for cre, bname in BILLING_RE:
            if cre.search(blob):
                billing = bname
                break
        name = None
        for j in range(i, max(-1, i - 4), -1):
            cand = heading_text(ls[j]) if j < len(ls) else ""
            if MEMB_KW.search(cand) or is_heading(ls[j]):
                name = cand[:120]
                break
        if not name:
            name = re.split(r"\s+[–—:-]\s+", ln)[0][:120]
        name = name.strip(" #*")
        if len(name) < 3 or junk_name.search(name):
            continue
        if re.fullmatch(r"memberships?|vip club|join|member plan", name, re.I):
            continue
        key = (name.lower(), price, billing)
        if key in seen:
            continue
        seen.add(key)
        # Contiguous quote from source lines so named-fee evidence can match markdown.
        fee_idx = next((k for k, x in enumerate(window) if PRICE_RE.search(x)), 0)
        name_idx = next((k for k, x in enumerate(window) if MEMB_KW.search(x) or normalize_simple(x) == normalize_simple(name)), 0)
        a, b = min(name_idx, fee_idx), max(name_idx, fee_idx) + 1
        desc = "\n".join(window[a:b+1])[:800]
        benefits = []
        for x in window:
            clean = re.sub(r"^#+\s*", "", x).strip(" *•-")
            if benefit_hint.search(clean) and 8 <= len(clean) <= 180:
                benefits.append(clean)
        memberships.append({
            "membership_name": name,
            "membership_price": price,
            "billing_period": billing,
            "minimum_commitment_months": None,
            "membership_description": desc,
            "benefits": benefits or None,
        })
    expl = f"deterministic_md_v1: found {len(memberships)} paid membership plan(s)."
    return {"explanation": expl, "memberships": memberships}


def atomic_json(path: Path, data):
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(data, ensure_ascii=False, indent=2))
    tmp.replace(path)


def process_page(row, path: Path, force: bool = True) -> str:
    capture = json.loads(path.read_text())
    body = (capture.get("response") or {}).get("data") or {}
    markdown = str(body.get("markdown") or "")
    if len(markdown.strip()) <= 50:
        return "skip_empty"
    target = OUT / "extraction" / str(row["input_row"]) / path.name
    if target.exists() and not force:
        ex = (json.loads(target.read_text()).get("extractions") or {})
        if "promotions" in ex and "memberships" in ex:
            return "skip_exists"
    source_url = (capture.get("request") or {}).get("url") or ""
    promo = parse_promotions(markdown, source_url)
    memb = parse_memberships(markdown)
    result = {
        "business_id": row["business_id"],
        "input_row": row["input_row"],
        "source_url": source_url,
        "capture_path": str(path.relative_to(ROOT)),
        "captured_at": capture.get("captured_at"),
        "api_url": capture.get("api_url"),
        "status": "dry_run_extracted",
        "extractions": {
            "promotions": promo,
            "memberships": memb,
            "services": {"services": []},
            "offers": {"offers": []},
        },
        "extraction_contracts": {"offers": OFFERS_CONTRACT},
        "updated_at": now(),
        "extractor": "deterministic_md_v1",
    }
    atomic_json(target, result)
    return "ok"


def main():
    force = os.getenv("CF208_FORCE_EXTRACT", "1") in {"1", "true", "yes"}
    batches = os.getenv("COSTFINDER_CANDIDATE_BATCH")
    batch_list = [batches] if batches in {"1", "2", "3"} else ["1", "2", "3"]
    ok = skip = empty = 0
    for b in batch_list:
        rows = json.loads((OUT / f"batches/batch_{b}/status.json").read_text())
        for row in rows:
            if row.get("status") != "pending_extraction":
                continue
            req = OUT / "requests" / str(row["input_row"])
            if not req.exists():
                continue
            for path in sorted(req.glob("scrape_*.json")):
                try:
                    st = process_page(row, path, force=force)
                except Exception as e:
                    print(json.dumps({"input_row": row["input_row"], "path": str(path), "status": "error", "error": str(e)}), flush=True)
                    continue
                if st == "ok":
                    ok += 1
                    if ok % 25 == 0:
                        print(json.dumps({"progress_ok": ok, "skip": skip, "empty": empty}), flush=True)
                elif st == "skip_exists":
                    skip += 1
                else:
                    empty += 1
    summary = {"ok": ok, "skip_exists": skip, "skip_empty": empty, "extractor": "deterministic_md_v1", "checked_at": now()}
    atomic_json(OUT / "deterministic_extract_summary.json", summary)
    print(json.dumps(summary), flush=True)


if __name__ == "__main__":
    main()
