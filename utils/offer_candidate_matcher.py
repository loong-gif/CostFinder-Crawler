"""Candidate matching helpers for evidence-driven offer updates."""
from __future__ import annotations

import re
from dataclasses import asdict, dataclass
from typing import Any, Dict, Iterable, List, Optional

from utils.offer_evidence_segments import extract_price_values, normalize_segment_text, normalize_url

_TOKEN_PATTERN = re.compile(r"[a-z0-9]+")


@dataclass(frozen=True)
class OfferMatchCandidate:
    candidate_offer_id: str
    match_score: float
    match_method: str
    score_breakdown: Dict[str, float]
    candidate_index: int
    service_name: str
    offer_raw_text: str

    def to_prompt_candidate(self, rank: int) -> Dict[str, Any]:
        return {
            "id": self.candidate_offer_id,
            "candidate_index": rank,
            "service_name": self.service_name,
            "offer_raw_text": self.offer_raw_text,
            "match_score": self.match_score,
            "match_method": self.match_method,
            "score_breakdown": self.score_breakdown,
        }


def normalize_tokens(value: Any) -> set[str]:
    return set(_TOKEN_PATTERN.findall(normalize_segment_text(value)))


def parse_numeric(value: Any) -> Optional[float]:
    if value in (None, ""):
        return None
    if isinstance(value, (int, float)):
        return float(value)
    found = extract_price_values(str(value))
    if found:
        return found[0]
    try:
        return float(str(value).replace(",", "").strip())
    except ValueError:
        return None


def candidate_price_values(candidate: Dict[str, Any]) -> List[float]:
    values: List[float] = []
    for field in ("regular_price", "discount_price", "original_price", "membership_price"):
        parsed = parse_numeric(candidate.get(field))
        if parsed is not None:
            values.append(parsed)
    for value in extract_price_values(candidate.get("offer_raw_text") or ""):
        values.append(value)
    deduped: List[float] = []
    for value in values:
        if value not in deduped:
            deduped.append(value)
    return deduped


def _service_score(segment: Dict[str, Any], candidate: Dict[str, Any]) -> float:
    mentions = {str(value).lower() for value in segment.get("service_mentions") or []}
    candidate_text = " ".join(
        str(candidate.get(field) or "")
        for field in ("display_service_name", "service_name", "canonical_service_name", "service_category", "offer_raw_text")
    )
    candidate_tokens = normalize_tokens(candidate_text)
    if not mentions:
        segment_tokens = normalize_tokens(segment.get("text") or "")
        overlap = segment_tokens & candidate_tokens
        return min(len(overlap) / 5, 0.7) if overlap else 0.0

    hits = 0
    for mention in mentions:
        mention_tokens = normalize_tokens(mention)
        if mention_tokens and mention_tokens <= candidate_tokens:
            hits += 1
    return min(hits / max(len(mentions), 1), 1.0)


def _price_score(segment: Dict[str, Any], candidate: Dict[str, Any]) -> float:
    segment_prices = [float(value) for value in segment.get("price_values") or []]
    if not segment_prices:
        segment_prices = extract_price_values(segment.get("text") or "")
    candidate_prices = candidate_price_values(candidate)
    if not segment_prices or not candidate_prices:
        return 0.0
    for left in segment_prices:
        for right in candidate_prices:
            if abs(left - right) < 0.01:
                return 1.0
    return 0.0


def _unit_score(segment: Dict[str, Any], candidate: Dict[str, Any]) -> float:
    terms = {str(value).lower() for value in segment.get("offer_terms") or []}
    unit = str(candidate.get("unit_type") or "").lower()
    if not terms or not unit:
        return 0.0
    if "per_unit" in terms and "unit" in unit:
        return 1.0
    if "per_syringe" in terms and "syringe" in unit:
        return 1.0
    if "per_vial" in terms and "vial" in unit:
        return 1.0
    if "monthly" in terms and unit in {"month", "monthly"}:
        return 1.0
    return 0.0


def _url_score(segment: Dict[str, Any], candidate: Dict[str, Any]) -> float:
    left = normalize_url(segment.get("source_url") or segment.get("source_url_normalized"))
    right = normalize_url(candidate.get("source_url"))
    if not left or not right:
        return 0.0
    return 1.0 if left == right else 0.0


def _text_overlap_score(segment: Dict[str, Any], candidate: Dict[str, Any]) -> float:
    segment_tokens = normalize_tokens(segment.get("text") or "")
    candidate_tokens = normalize_tokens(candidate.get("offer_raw_text") or "")
    if not segment_tokens or not candidate_tokens:
        return 0.0
    return min(len(segment_tokens & candidate_tokens) / max(len(candidate_tokens), 1), 1.0)


def score_offer_candidate(segment: Dict[str, Any], candidate: Dict[str, Any]) -> Dict[str, float]:
    return {
        "url": _url_score(segment, candidate),
        "service": _service_score(segment, candidate),
        "price": _price_score(segment, candidate),
        "unit": _unit_score(segment, candidate),
        "text_overlap": _text_overlap_score(segment, candidate),
    }


def weighted_score(breakdown: Dict[str, float]) -> float:
    score = (
        breakdown.get("url", 0.0) * 0.30
        + breakdown.get("service", 0.0) * 0.30
        + breakdown.get("price", 0.0) * 0.25
        + breakdown.get("unit", 0.0) * 0.10
        + breakdown.get("text_overlap", 0.0) * 0.05
    )
    return round(min(score, 1.0), 4)


def infer_match_method(breakdown: Dict[str, float]) -> str:
    if breakdown.get("url") == 1.0 and breakdown.get("service", 0.0) >= 1.0 and breakdown.get("price") == 1.0:
        return "url_service_price"
    if breakdown.get("url") == 1.0 and breakdown.get("service", 0.0) >= 1.0:
        return "url_service"
    if breakdown.get("service", 0.0) >= 1.0 and breakdown.get("price") == 1.0:
        return "service_price"
    if breakdown.get("url") == 1.0:
        return "same_url"
    return "text_similarity"


def rank_offer_candidates(
    segment: Dict[str, Any],
    candidates: Iterable[Dict[str, Any]],
    *,
    min_score: float = 0.25,
    limit: int = 10,
) -> List[OfferMatchCandidate]:
    ranked: List[OfferMatchCandidate] = []
    for index, candidate in enumerate(candidates, start=1):
        candidate_id = str(candidate.get("id") or candidate.get("candidate_offer_id") or "").strip()
        if not candidate_id:
            continue
        breakdown = score_offer_candidate(segment, candidate)
        score = weighted_score(breakdown)
        if score < min_score:
            continue
        ranked.append(
            OfferMatchCandidate(
                candidate_offer_id=candidate_id,
                match_score=score,
                match_method=infer_match_method(breakdown),
                score_breakdown=breakdown,
                candidate_index=index,
                service_name=str(candidate.get("display_service_name") or candidate.get("service_name") or ""),
                offer_raw_text=str(candidate.get("offer_raw_text") or ""),
            )
        )
    ranked.sort(key=lambda item: (-item.match_score, item.candidate_index))
    return ranked[:limit]


def to_match_candidate_records(
    segment_id: str,
    change_event_id: str,
    matches: Iterable[OfferMatchCandidate],
) -> List[Dict[str, Any]]:
    records: List[Dict[str, Any]] = []
    for rank, match in enumerate(matches, start=1):
        records.append(
            {
                "change_event_id": change_event_id,
                "segment_id": segment_id,
                "candidate_offer_id": match.candidate_offer_id,
                "match_score": match.match_score,
                "match_method": match.match_method,
                "score_breakdown": match.score_breakdown,
                "rank": rank,
                "is_selected": False,
            }
        )
    return records
