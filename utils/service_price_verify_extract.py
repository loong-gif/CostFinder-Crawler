from __future__ import annotations

import re
from decimal import Decimal
from typing import Optional

from utils.service_price_verify_hits import (
    NAV_NOISE_RE,
    SERVICE_ALIASES,
    UNIT_HINT_RE,
    ExtractResult,
    PriceHit,
    classify_line,
    extract_hits_from_line,
    line_matches_service,
    page_implies_service,
    parse_price_hits_on_line,
    prices_equal,
    score_hit,
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
