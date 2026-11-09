"""offer 条件：从卡片原文取条件句，校验 LLM 给出的条件与来源证据。"""
from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass
from typing import Any, Mapping

_BOOLEAN_CONDITION_SIGNAL = re.compile(
    r"(?:membership\s+required|new\s+(?:patient|client|customer)s?\s+only|"
    r"first[-\s]?time\s+(?:patient|client|customer)s?\s+only)",
    re.IGNORECASE,
)
_CONDITION_SIGNAL = re.compile(
    r"\b(?:valid|only|limit(?:ed)?|cannot|can't|not\s+(?:valid|redeemable|combinable)|exclud(?:es?|ing)|must|"
    r"requires?|required|minimum|min\.|at\s+least|expires?|through|until|while\s+supplies|per\s+(?:patient|person|"
    r"client|customer|household)|one\s+per|new\s+(?:patients?|clients?|customers?)|first[-\s]time|restrictions?|"
    r"eligible|redeem(?:able)?|purchase\s+of|package\s+of|when\s+purchased|applies|in\s+person|by\s+phone|appointment|"
    r"includes?)\b",
    re.IGNORECASE,
)
# 条款段落开头的「标题 + 脚注符号」前缀（例如「Botox New Patient Promotion ‡‡Valid on ...」）
_TERMS_PREFIX = re.compile(r"^.*?[‡†¶§◊✢✴ⱽǂ^#*]+\s*(?=[A-Z])")
_SENTENCE_SPLIT = re.compile(r"(?<=[.!?])\s+(?=[A-Z(])")


@dataclass(frozen=True)
class OfferEvidenceIssue:
    field: str
    reason: str
    value: Any

    def as_dict(self) -> dict[str, Any]:
        return {"field": self.field, "reason": self.reason, "value": self.value}


def _normalize_text(value: Any) -> str:
    text = unicodedata.normalize("NFKC", str(value or "")).casefold()
    text = text.replace("–", "-").replace("—", "-")
    return re.sub(r"[^\w$%./+-]+", " ", text).strip()


def card_conditions(card: str, offer_raw_text: str) -> list[str]:
    """offer 所在卡片中的条件句（原文）：按句拆分，去掉条款标题与脚注符号前缀，跳过 offer 原文所在的句子，
    保留含条件措辞的句子。结果只取决于卡片原文，不随 LLM 回答变化。"""
    raw_key = _normalize_text(offer_raw_text)
    conditions: list[str] = []
    for line in str(card or "").splitlines():
        for sentence in _SENTENCE_SPLIT.split(line.strip()):
            text = " ".join(_TERMS_PREFIX.sub("", sentence).split())
            if not text or (raw_key and raw_key in _normalize_text(text)) or not _CONDITION_SIGNAL.search(text):
                continue
            if text not in conditions:
                conditions.append(text)
    return conditions


def sanitize_conditions(
    conditions: Any,
    source: str,
) -> tuple[list[str] | None, tuple[OfferEvidenceIssue, ...]]:
    """Validate source evidence and return only requirement strings.

    Extraction still supplies ``requirement`` + ``evidence`` objects so the
    evidence gate can reject unsupported claims.  Persistence deliberately
    keeps only the requirement text because the database column is ``text[]``.
    """
    if conditions is None:
        return None, (OfferEvidenceIssue("conditions", "missing_conditions", None),)
    if not isinstance(conditions, list):
        return None, (OfferEvidenceIssue("conditions", "must_be_array", conditions),)

    normalized_source = _normalize_text(source)
    output: list[str] = []
    seen: set[str] = set()
    issues: list[OfferEvidenceIssue] = []
    for index, raw in enumerate(conditions):
        field = f"conditions[{index}]"
        if not isinstance(raw, Mapping):
            issues.append(OfferEvidenceIssue(field, "must_be_object", raw))
            continue
        extra_fields = set(raw) - {"requirement", "evidence"}
        if extra_fields:
            issues.append(OfferEvidenceIssue(field, "unexpected_fields", sorted(extra_fields)))
            continue
        raw_requirement = raw.get("requirement")
        raw_evidence = raw.get("evidence")
        if not isinstance(raw_requirement, str):
            issues.append(OfferEvidenceIssue(f"{field}.requirement", "must_be_string", raw_requirement))
            continue
        if not isinstance(raw_evidence, str):
            issues.append(OfferEvidenceIssue(f"{field}.evidence", "must_be_string", raw_evidence))
            continue
        requirement = raw_requirement.strip()
        evidence = raw_evidence.strip()
        if not requirement:
            issues.append(OfferEvidenceIssue(f"{field}.requirement", "missing_requirement", raw.get("requirement")))
            continue
        if not evidence:
            issues.append(OfferEvidenceIssue(f"{field}.evidence", "missing_evidence", raw.get("evidence")))
            continue
        if _BOOLEAN_CONDITION_SIGNAL.search(requirement):
            issues.append(OfferEvidenceIssue(f"{field}.requirement", "duplicates_boolean_field", requirement))
            continue
        if not normalized_source or _normalize_text(evidence) not in normalized_source:
            issues.append(OfferEvidenceIssue(f"{field}.evidence", "not_supported_by_source", evidence))
            continue
        key = _normalize_text(requirement)
        if key in seen:
            continue
        seen.add(key)
        output.append(requirement)
    return output, tuple(issues)
