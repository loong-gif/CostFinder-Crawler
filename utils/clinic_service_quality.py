"""Deterministic evidence rules for the ``clinic_services`` repair job."""
from __future__ import annotations

import re
from dataclasses import dataclass
from decimal import Decimal
from typing import Any, Iterable

CANONICAL_SERVICES = (
    "Botox", "Dysport", "Daxxify", "Xeomin", "Jeuveau", "Letybo",
    "Dermal Filler", "Juvéderm", "Radiesse", "Sculptra",
)
_PATTERNS = {
    "Botox": r"\bbotox\b", "Dysport": r"\bdysport\b", "Daxxify": r"\bdaxxify\b",
    "Xeomin": r"\bxeomin\b", "Jeuveau": r"\bjeuveau\b", "Letybo": r"\bletybo\b",
    "Dermal Filler": r"\b(?:dermal\s+)?fillers?\b", "Juvéderm": r"\bjuv[ée]derm\b",
    "Radiesse": r"\bradiesse\b", "Sculptra": r"\bsculptra\b",
}
_UNITS = {
    "unit": r"(?:per|/)\s*units?\b", "syringe": r"(?:per|/)\s*(?:half[ -]?)?syringe\b",
    "vial": r"(?:per|/)\s*vial\b", "treatment": r"(?:per|/)\s*treatment\b",
}
_PROMO = re.compile(r"\b(?:special|promo(?:tion)?|sale|discount|member(?:ship)?|new\s+(?:patient|client)|limited\s+time|starting\s+at|from)\b", re.I)
_PRICE = re.compile(r"\$\s*(\d+(?:\.\d{1,2})?)\b")


@dataclass(frozen=True)
class EvidenceDecision:
    regular_price: Decimal | None
    unit_type: str
    raw_text: str
    verified: bool
    reason: str


def service_category(service_name: str) -> str:
    return "Neurotoxin" if service_name in CANONICAL_SERVICES[:6] else "Filler"


def service_mentions(text: str) -> list[str]:
    return [name for name, pattern in _PATTERNS.items() if re.search(pattern, text, re.I)]


def _lines(text: str) -> Iterable[str]:
    for value in re.split(r"[\r\n]+", text or ""):
        line = re.sub(r"\s+", " ", value).strip()
        if line:
            yield line


def _unit(line: str) -> str:
    for unit, pattern in _UNITS.items():
        if re.search(pattern, line, re.I):
            return unit
    return "others"


def evaluate_service(service_name: str, evidence_texts: Iterable[str]) -> EvidenceDecision:
    """Return the first explicit non-promotional catalogue price, if any.

    A price line must name exactly one target service; this prevents a Botox
    record from borrowing Dysport's amount in a shared pricing list.
    """
    fallback = ""
    for text in evidence_texts:
        for line in _lines(str(text or "")):
            mentioned = service_mentions(line)
            if service_name not in mentioned:
                continue
            fallback = fallback or line
            unit_type = _unit(line)
            prices = _PRICE.findall(line)
            if len(mentioned) != 1 or len(prices) != 1 or unit_type == "others" or _PROMO.search(line):
                continue
            return EvidenceDecision(Decimal(prices[0]), unit_type, line, True, "priced_evidence")
    if fallback:
        return EvidenceDecision(None, _unit(fallback), fallback, True, "service_existence_evidence")
    return EvidenceDecision(None, "others", "", False, "missing_service_evidence")
