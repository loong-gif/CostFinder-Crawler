from __future__ import annotations
import math
import re
from typing import Any, Mapping
from utils.promo_extraction import traceable
from utils.render_review import claim_matches
from utils.rendered_visibility_gate import filter_visible_membership_benefits, normalize_claim


def _markdown_quote(value: str) -> str:
    return normalize_claim(re.sub(r"(?m)^\s{0,3}#{1,6}\s*", "", value))


def _visible_named_fee(name: str, amount: str, evidence: str, quote: str,
                       rendered_visibility: Mapping[str, Any] | None) -> bool:
    normalized_name = normalize_claim(name)
    normalized_quote = _markdown_quote(quote)
    normalized_evidence = normalize_claim(evidence)
    if (normalized_quote and normalized_name in normalized_quote
            and re.search(r"[$€£]\s*" + amount + r"(?:\.0{1,2})?(?![\d.])", normalized_quote.replace(",", ""))
            and normalized_quote in normalized_evidence):
        return True

    line_evidence = "\n".join(line for line in evidence.splitlines()
                               if normalized_name and normalized_name in normalize_claim(line))
    fee_pattern = re.compile(r"[$€£]\s*" + amount + r"(?:\.0{1,2})?(?![\d.])")
    if fee_pattern.search(line_evidence.replace(",", "")):
        return True

    if not isinstance(rendered_visibility, Mapping) or rendered_visibility.get("status") != "page_accessible":
        return False
    nodes = [node for node in (rendered_visibility.get("capture") or {}).get("nodes", [])
             if isinstance(node, Mapping) and node.get("visible") is True]
    heading_tags = {"H1", "H2", "H3", "H4", "H5", "H6"}
    for index, node in enumerate(nodes):
        text = str(node.get("text") or "")
        if normalized_name not in normalize_claim(text):
            continue
        rects = node.get("rects") or [{}]
        name_y = rects[0].get("y")
        if not isinstance(name_y, (int, float)):
            continue
        for candidate in nodes[index:index + 12]:
            if candidate is not node and ((candidate.get("ancestors") or [{}])[0].get("tag", "").upper() in heading_tags):
                break
            fee_text = str(candidate.get("text") or "").replace(",", "")
            if not fee_pattern.search(fee_text):
                continue
            fee_rects = candidate.get("rects") or [{}]
            fee_y = fee_rects[0].get("y")
            if isinstance(fee_y, (int, float)) and -24 <= fee_y - name_y <= 240:
                return True
    return False


def prepare_membership_write(item: dict[str, Any], evidence: str, rendered_visibility: Mapping[str, Any] | None = None,
                             *, source_claims: list[str] | None = None, ocr_complete: bool = True) -> dict[str, Any]:
    if source_claims is None:
        source_claims = item.get('source_claims')
    name = str(item.get('membership_name') or item.get('plan_name') or '').strip()
    price = item.get('membership_price')
    quote = str(item.get('membership_description') or item.get('evidence_quote') or '').strip()
    benefits = item.get('benefits')
    verified = None
    if isinstance(benefits, list) and benefits:
        values = [str(value.get('desc') or '') if isinstance(value, dict) else str(value) for value in benefits]
        # 允许改写，但每条权益必须能在页面上追溯（金额、百分比与数量都在页面上）
        values = [value.strip() for value in values if value.strip() and traceable(value, evidence)]
        gate = filter_visible_membership_benefits(values, rendered_visibility)
        if gate.accepted:
            verified = list(gate.content)
    try:
        fee = float(price)
    except (TypeError, ValueError):
        return {'accepted': False, 'reason': 'missing_membership_or_price', 'benefits': verified, 'complete': False}
    amount = re.escape(f'{fee:g}')
    fee_supported = (bool(name) and not isinstance(price, bool) and math.isfinite(fee) and fee > 0
                     and _visible_named_fee(name, amount, evidence, quote, rendered_visibility))
    # 来源清单独立于抽取结果，避免只验证模型已经输出的权益。
    # 来源清单中的每条权益都要有对应的抽取权益，每条抽取权益也要出自来源清单（允许改写）
    missing_claims = [claim for claim in source_claims or []
                      if not normalize_claim(claim) or not traceable(claim, evidence)
                      or not claim_matches({'line': claim}, verified or [])]
    missing_claims.extend(value for value in verified or []
                          if not claim_matches({'line': value}, source_claims or []))
    complete = (bool(verified) and len(verified) == len(benefits or [])
                and bool(source_claims) and not missing_claims
                and ocr_complete
                and isinstance(rendered_visibility, Mapping)
                and rendered_visibility.get('complete') is True)
    return {'accepted': fee_supported,
            'reason': ('validated' if complete else 'membership_benefits_unverified') if fee_supported else 'membership_fee_not_supported_by_named_evidence',
            'benefits': verified if complete or not fee_supported else None,
            'verified_benefits': verified, 'missing_claims': missing_claims,
            'complete': complete, 'membership_price': fee}
