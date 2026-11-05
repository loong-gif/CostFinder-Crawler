"""Browser-render review of pages whose visibility could not be judged from crawler HTML.

``needs_review`` pages are queued, rendered in ego-lite, and each evidence line is checked against the
rendered text. A page is confirmed only when its price or discount tokens appear in text a visitor can see.
"""
from __future__ import annotations

import hashlib
import re
from pathlib import Path
from typing import Any, Dict, Iterable, List, Mapping, Optional

import requests

from utils.paddle_ocr_markdown import ocr_lines

AMOUNT_RE = re.compile(r"\$\s?\d[\d,]*(?:\.\d{1,2})?")
PERCENT_RE = re.compile(r"\d{1,3}(?:\.\d+)?\s?%")
VISIBLE_FRACTION = 0.5
MAX_ATTEMPTS = 3


def normalize_anchor(token: str) -> str:
    """Same normalisation as the in-page evaluator: lower case, no spaces/commas, no trailing .00."""
    value = re.sub(r"\s+", "", token.lower()).replace(",", "")
    return re.sub(r"(\d)\.00(?!\d)", r"\1", value)


def claim_anchors(line: str) -> List[str]:
    found: List[str] = []
    for regex in (AMOUNT_RE, PERCENT_RE):
        for match in regex.finditer(str(line)):
            anchor = normalize_anchor(match.group(0))
            if anchor not in found:
                found.append(anchor)
    return found


def item_id(url: str) -> str:
    return hashlib.sha1(url.encode("utf-8")).hexdigest()[:12]


def build_items(decisions: Iterable[Mapping[str, Any]], done: Optional[Mapping[str, Mapping[str, Any]]] = None
                ) -> List[Dict[str, Any]]:
    """Queue entries for kept ``needs_review`` pages; pages already rendered are not queued again."""
    done = done or {}
    items: List[Dict[str, Any]] = []
    seen = set()
    for record in decisions:
        url = record["url"]
        if record.get("decision") == "skip" or record.get("status") != "needs_review" or url in seen:
            continue
        seen.add(url)
        previous = done.get(url)
        if previous and previous.get("verdict") in ("verified", "not_visible", "manual_review"):
            continue
        claims = [{"id": i, "line": line[:240], "anchors": claim_anchors(line)}
                  for i, line in enumerate(record.get("evidence") or [])]
        items.append({"id": item_id(url), "url": url, "business_id": record.get("business_id"),
                      "business": record.get("business"), "claims": claims})
    return items


def shard(items: List[Dict[str, Any]], count: int) -> List[List[Dict[str, Any]]]:
    """Round-robin so every shard gets a similar mix of sites and sizes."""
    shards: List[List[Dict[str, Any]]] = [[] for _ in range(max(1, count))]
    for index, item in enumerate(items):
        shards[index % len(shards)].append(item)
    return [s for s in shards if s]


def page_verdict(record: Mapping[str, Any]) -> Dict[str, Any]:
    """Collapse the in-page claim results to one verdict for the URL."""
    if record.get("error") or not record.get("result"):
        return {"verdict": "page_unavailable", "reason": str(record.get("error") or "no_result")[:160]}
    claims = [c for c in record["result"].get("claims", []) if c.get("anchors")]
    if not claims:
        # The evidence lines carry no price or percent token, so the renderer has nothing to match.
        # That says nothing about visibility: neither promote nor drop the page.
        return {"verdict": "manual_review", "reason": "no_price_tokens_to_check"}
    visible = [c for c in claims if c.get("status") == "visible_match"]
    hidden = [c for c in claims if c.get("status") == "dom_only_hidden"]
    ratio = len(visible) / len(claims)
    if visible and ratio >= VISIBLE_FRACTION:
        return {"verdict": "verified", "reason": f"{len(visible)}_of_{len(claims)}_claims_visible"}
    if hidden and not visible:
        return {"verdict": "not_visible", "reason": "claims_only_in_hidden_elements"}
    if visible:
        return {"verdict": "not_visible", "reason": f"only_{len(visible)}_of_{len(claims)}_claims_visible"}
    return {"verdict": "not_visible", "reason": "claims_not_found_in_rendered_page"}


def apply_verdicts(decisions: List[Dict[str, Any]], verdicts: Mapping[str, Mapping[str, Any]]) -> List[Dict[str, Any]]:
    """Update decisions in place: rendered-verified pages become ``add``, rendered-hidden ones are skipped."""
    for record in decisions:
        verdict = verdicts.get(record["url"])
        if not verdict or record.get("status") != "needs_review":
            continue
        record["render_review"] = {k: verdict.get(k) for k in ("verdict", "reason", "checked_at", "final_url", "screenshot")}
        if verdict.get("verdict") == "verified" and record.get("decision") != "skip":
            record["decision"] = "add"
            record.setdefault("reasons", []).append("render_verified")
        elif verdict.get("verdict") == "not_visible":
            record["decision"] = "skip"
            record.setdefault("reasons", []).append("rendered_not_visible")
        # page_unavailable keeps add_with_review and is retried (up to MAX_ATTEMPTS);
        # manual_review keeps add_with_review and waits for a person
    return decisions


# ---- deep review: expanded panels and image text ----
LINK_RE = re.compile(r"\[([^\]]*)\]\([^)]*\)")
FUZZY_THRESHOLD = 88
MIN_SUBSTANTIVE_WORDS = 4


def plain_text(value: str) -> str:
    text = LINK_RE.sub(r"\1", str(value))
    text = re.sub(r"[*#>|_~`]+", " ", text)
    return re.sub(r"\s+", " ", text).strip().lower()


def corpus_text(texts: Iterable[str]) -> str:
    joined = " ".join(plain_text(t) for t in texts)
    joined = re.sub(r"(\d),(\d{3})", r"\1\2", joined)
    joined = re.sub(r"\$\s+", "$", joined)
    joined = re.sub(r"\s+%", "%", joined)
    return re.sub(r"(\d)\.00(?!\d)", r"\1", joined)


def anchor_in(corpus: str, anchor: str) -> bool:
    start = corpus.find(anchor)
    while start != -1:
        nxt = corpus[start + len(anchor): start + len(anchor) + 1]
        if not nxt or not nxt.isdigit():
            return True
        start = corpus.find(anchor, start + 1)
    return False


def is_substantive(claim: Mapping[str, Any]) -> bool:
    if claim.get("anchors"):
        return True
    return len(plain_text(claim.get("line", "")).split()) >= MIN_SUBSTANTIVE_WORDS


def claim_matches(claim: Mapping[str, Any], corpus: str) -> bool:
    from rapidfuzz import fuzz

    if claim.get("anchors"):
        return all(anchor_in(corpus, a) for a in claim["anchors"])
    text = plain_text(claim.get("line", ""))[:160]
    return fuzz.partial_ratio(text, corpus) >= FUZZY_THRESHOLD


def deep_verdict(claims: List[Mapping[str, Any]], visible_texts: Iterable[str], image_texts: Iterable[str] = ()
                 ) -> Dict[str, Any]:
    """Verdict from the text visible after expanding panels, plus text read from visible images."""
    page_corpus = corpus_text(visible_texts)
    image_corpus = corpus_text(image_texts)
    judged = [c for c in claims if is_substantive(c)]
    if not judged:
        return {"verdict": "not_visible", "reason": "no_substantive_evidence_line"}
    in_text = [c for c in judged if claim_matches(c, page_corpus)]
    in_image = [c for c in judged if c not in in_text and image_corpus and claim_matches(c, image_corpus)]
    matched = len(in_text) + len(in_image)
    if matched / len(judged) >= VISIBLE_FRACTION:
        source = "image_text" if in_image and not in_text else "expanded_page_text" if in_text else "page_text"
        return {"verdict": "verified", "reason": f"{matched}_of_{len(judged)}_claims_visible", "source": source}
    return {"verdict": "not_visible", "reason": f"only_{matched}_of_{len(judged)}_claims_visible_after_expanding"}


def ocr_image(url: str, image_dir: Path) -> str:
    target = image_dir / (hashlib.sha1(url.encode()).hexdigest()[:16] + ".img")
    if not target.exists():
        response = requests.get(url, timeout=30, headers={"User-Agent": "Mozilla/5.0"})
        response.raise_for_status()
        target.write_bytes(response.content)
    return "\n".join(ocr_lines(target.read_bytes()))


def build_deep_items(decisions: Iterable[Mapping[str, Any]]) -> List[Dict[str, Any]]:
    """Pages whose render verdict is not ``verified``: collapsed panels, image promotions, blocked loads."""
    items: List[Dict[str, Any]] = []
    seen = set()
    for record in decisions:
        review = record.get("render_review") or {}
        if record.get("status") != "needs_review" or review.get("verdict") in (None, "verified") or record["url"] in seen:
            continue
        seen.add(record["url"])
        claims = [{"id": i, "line": line[:240], "anchors": claim_anchors(line)}
                  for i, line in enumerate(record.get("evidence") or [])]
        items.append({"id": item_id(record["url"]), "url": record["url"], "business_id": record.get("business_id"),
                      "business": record.get("business"), "claims": claims})
    return items
