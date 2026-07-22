"""
LLM-driven offer extraction helpers.
"""
from __future__ import annotations

import json
import os
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Protocol

import requests

from crawler.promo_site_crawler import build_llm_ready_content, filter_page_segments, normalize_segment_text
from utils.membership_plans import (
    extract_membership_plans_for_row,
    normalize_membership_plan_refs,
)
from utils.membership_paths import is_membership_page_url
from utils.offer_scope_filter import filter_service_offers
from utils.service_category_lookup import resolve_service_category

SERVICE_NAME_DICT_PATH = Path(__file__).resolve().parents[1] / "service_name_dict.json"
SCHEMA_DIR = Path(__file__).resolve().parents[1] / "schema"
PROMOTION_EXTRACTION_SCHEMA_PATH = SCHEMA_DIR / "promotion_extraction_schema.json"
SERVICE_EXTRACTION_SCHEMA_PATH = SCHEMA_DIR / "service_extraction_schema.json"

OFFER_OUTPUT_FIELDS = [
    "service_name",
    "display_service_name",
    "canonical_service_name",
    "service_category",
    "template_type",
    "offer_content",
    "original_price",
    "discount_price",
    "discount_amount",
    "discount_percent",
    "unit_type",
    "offer_raw_text",
    "evidence_segments",
]


def parse_json_payload(value: Any, default: Any) -> Any:
    if isinstance(value, (dict, list)):
        return value
    if value is None:
        return default
    raw = str(value).strip()
    if not raw:
        return default
    if raw.startswith("```"):
        raw = re.sub(r"^```(?:json)?\s*|\s*```$", "", raw, flags=re.DOTALL).strip()
    try:
        return json.loads(raw)
    except json.JSONDecodeError:
        return default


def build_text_segments(page_content: str) -> List[Dict[str, Any]]:
    blocks = []
    seen_indexes: set[int] = set()
    for fallback_idx, part in enumerate(re.split(r"(?:===|\n{2,})", page_content or "")):
        text = normalize_segment_text(part)
        if not text:
            continue
        match = re.match(r"^\[SEGMENT\s+(\d+)\]", text, flags=re.IGNORECASE)
        index = int(match.group(1)) if match else fallback_idx
        while index in seen_indexes:
            index += 1
        seen_indexes.add(index)
        blocks.append(
            {
                "index": index,
                "tag": "text_block",
                "text": text,
                "text_length": len(text),
            }
        )
    if not blocks and page_content:
        text = normalize_segment_text(page_content)
        if text:
            blocks.append({"index": 0, "tag": "text_block", "text": text, "text_length": len(text)})
    return blocks


def load_service_name_dictionary() -> Dict[str, Any]:
    try:
        payload = json.loads(SERVICE_NAME_DICT_PATH.read_text(encoding="utf-8"))
    except (FileNotFoundError, json.JSONDecodeError):
        return {"standardized_names": ["Others"], "aliases": {}, "display_service_names": []}
    names = payload.get("standardized_names") or []
    if "Others" not in names:
        names.append("Others")
    payload["standardized_names"] = names
    payload.setdefault("aliases", {})
    payload.setdefault("display_service_names", [])
    return payload


def get_standardized_service_names() -> List[str]:
    return [str(item).strip() for item in load_service_name_dictionary().get("standardized_names", []) if str(item).strip()]


def get_display_service_names() -> List[str]:
    dictionary = load_service_name_dictionary()
    names = [str(item).strip() for item in dictionary.get("display_service_names", []) if str(item).strip()]
    aliases = [str(item).strip() for item in dictionary.get("aliases", {}).keys() if str(item).strip()]
    return sorted(set(names + aliases))


def _clean_text(value: Any) -> str:
    return re.sub(r"\s+", " ", str(value or "").strip())


def _normalize_lookup_text(value: Any) -> str:
    return normalize_segment_text(_clean_text(value)).lower()


def _service_dictionary_indexes() -> Dict[str, Any]:
    dictionary = load_service_name_dictionary()
    canonical_by_normalized: Dict[str, str] = {}
    for name in dictionary.get("standardized_names", []):
        cleaned = _clean_text(name)
        if cleaned:
            canonical_by_normalized[_normalize_lookup_text(cleaned)] = cleaned

    alias_by_normalized: Dict[str, str] = {}
    for alias, canonical in dictionary.get("aliases", {}).items():
        alias_key = _normalize_lookup_text(alias)
        canonical_key = _normalize_lookup_text(canonical)
        if alias_key and canonical_key in canonical_by_normalized:
            alias_by_normalized[alias_key] = canonical_by_normalized[canonical_key]

    return {
        "canonical_by_normalized": canonical_by_normalized,
        "alias_by_normalized": alias_by_normalized,
    }


def canonicalize_service_name(*values: Any) -> str:
    indexes = _service_dictionary_indexes()
    canonical_by_normalized = indexes["canonical_by_normalized"]
    alias_by_normalized = indexes["alias_by_normalized"]

    normalized_values = [_normalize_lookup_text(value) for value in values if _clean_text(value)]
    for text in normalized_values:
        if text in canonical_by_normalized:
            return canonical_by_normalized[text]
        if text in alias_by_normalized:
            return alias_by_normalized[text]

    for text in normalized_values:
        for alias, canonical in alias_by_normalized.items():
            if re.search(rf"(?<![a-z0-9]){re.escape(alias)}(?![a-z0-9])", text):
                return canonical

    return canonical_by_normalized.get("others", "Others")


def normalize_service_identity(record: Dict[str, Any]) -> None:
    display = _clean_text(
        record.get("display_service_name")
        or record.get("raw_service_name")
        or record.get("service_name")
        or record.get("canonical_service_name")
    )
    canonical = canonicalize_service_name(
        record.get("canonical_service_name"),
        record.get("service_name"),
        display,
        record.get("offer_raw_text"),
        record.get("offer_content"),
    )
    record["display_service_name"] = display
    record["canonical_service_name"] = canonical
    record["service_name"] = canonical


def chunk_segments(segments: List[Dict[str, Any]], chunk_size: int) -> List[List[Dict[str, Any]]]:
    if chunk_size <= 0:
        return [segments]
    return [segments[index:index + chunk_size] for index in range(0, len(segments), chunk_size)]


def merge_offer_payloads(payloads: List[Dict[str, Any]]) -> Dict[str, Any]:
    offers: List[Dict[str, Any]] = []
    for payload in payloads:
        items = payload.get("offers", []) if isinstance(payload, dict) else []
        if isinstance(items, list):
            offers.extend(item for item in items if isinstance(item, dict))
    return {"offers": offers}


def load_extraction_schema(path: Path) -> Dict[str, Any]:
    return json.loads(path.read_text(encoding="utf-8"))


def load_promotion_extraction_schema() -> Dict[str, Any]:
    return load_extraction_schema(PROMOTION_EXTRACTION_SCHEMA_PATH)


def load_service_extraction_schema() -> Dict[str, Any]:
    return load_extraction_schema(SERVICE_EXTRACTION_SCHEMA_PATH)


def merge_promotion_payloads(payloads: List[Dict[str, Any]]) -> Dict[str, Any]:
    title = ""
    is_new_customer = False
    offers: List[Dict[str, Any]] = []
    for payload in payloads:
        promotion = payload.get("promotion") if isinstance(payload, dict) else {}
        if not isinstance(promotion, dict) or not promotion:
            continue
        if not title:
            title = str(promotion.get("promotion_title") or "").strip()
        if promotion.get("is_new_customer_required"):
            is_new_customer = True
        chunk_offers = promotion.get("offers")
        if isinstance(chunk_offers, list):
            offers.extend(item for item in chunk_offers if isinstance(item, dict))
    if not title and not offers:
        return {"promotion": {}}
    return {
        "promotion": {
            "promotion_title": title or "Promotion",
            "is_new_customer_required": is_new_customer,
            "offers": offers,
        }
    }


def _template_type_for_price_model(price_model: str) -> str:
    mapping = {"from": "FROM_PRICE", "total": "FIXED_PRICE", "per_unit": "FIXED_PRICE"}
    return mapping.get(str(price_model or "").strip(), "")


def promotion_payload_to_offers(payload: Any, allowed_indexes: set[int]) -> List[Dict[str, Any]]:
    data = parse_json_payload(payload, {})
    promotion = data.get("promotion") if isinstance(data, dict) else {}
    if not isinstance(promotion, dict) or not promotion:
        return []

    promo_title = str(promotion.get("promotion_title") or "").strip()
    offers_out: List[Dict[str, Any]] = []
    for offer in promotion.get("offers") or []:
        if not isinstance(offer, dict):
            continue
        items = offer.get("items")
        if not isinstance(items, list) or not items:
            items = [{"item_name": promo_title or "Offer"}]
        for item in items:
            if not isinstance(item, dict):
                continue
            item_name = str(item.get("item_name") or promo_title or "Offer").strip()
            record = {
                "service_name": "",
                "display_service_name": item_name,
                "canonical_service_name": "",
                "service_category": "",
                "template_type": _template_type_for_price_model(str(offer.get("price_model") or "")),
                "offer_content": promo_title,
                "original_price": offer.get("regular_price") if offer.get("regular_price") is not None else "",
                "discount_price": offer.get("discount_price") if offer.get("discount_price") is not None else "",
                "discount_amount": "",
                "discount_percent": offer.get("discount_percent") if offer.get("discount_percent") is not None else "",
                "unit_type": str(item.get("unit_type") or "").strip(),
                "offer_raw_text": item_name,
                "evidence_segments": [],
            }
            offers_out.append(normalize_offer_record(record, allowed_indexes))
    return filter_service_offers(offers_out)


def build_promotion_extraction_messages(
    row: Dict[str, Any],
    selected_segments: List[Dict[str, Any]],
) -> List[Dict[str, str]]:
    segment_lines = [f"[{item['index']}] {item['text']}" for item in selected_segments]
    return [
        {
            "role": "system",
            "content": (
                "You extract promotional pricing from aesthetic medspa page evidence only. "
                "Do not infer missing values. Ignore navigation, CTAs, and membership tier plan fees. "
                "Do not extract free consultations or retail catalog SKUs as treatment offers. "
                "Price ranges without a single number should use null prices or be omitted."
            ),
        },
        {
            "role": "user",
            "content": (
                f"Extract promotion data for {row.get('domain_name', '')} {row.get('subpage_url', '')}.\n"
                f"Evidence:\n{json.dumps(segment_lines, ensure_ascii=False, indent=2)}"
            ),
        },
    ]


class StructuredLLMClient(Protocol):
    model: str

    def create_json_response(
        self,
        messages: List[Dict[str, str]],
        *,
        json_schema: Optional[Dict[str, Any]] = None,
    ) -> Dict[str, Any]: ...


def load_filtered_segments(row: Dict[str, Any]) -> List[Dict[str, Any]]:
    payload = parse_json_payload(row.get("page_segments_filtered"), [])
    if isinstance(payload, list) and payload:
        return [item for item in payload if isinstance(item, dict) and item.get("text")]

    raw_payload = parse_json_payload(row.get("page_segments_raw"), [])
    if isinstance(raw_payload, list) and raw_payload:
        filtered, _ = filter_page_segments([item for item in raw_payload if isinstance(item, dict) and item.get("text")])
        return filtered

    filtered, _ = filter_page_segments(build_text_segments(row.get("page_content", "")))
    return filtered


def build_candidate_block_selection_messages(row: Dict[str, Any], segments: List[Dict[str, Any]]) -> List[Dict[str, str]]:
    segment_lines = [f"[{item['index']}] {item['text']}" for item in segments]
    user_payload = {
        "domain_name": row.get("domain_name", ""),
        "subpage_url": row.get("subpage_url", ""),
        "segments": segment_lines,
    }
    return [
        {
            "role": "system",
            "content": (
                "You select evidence blocks for aesthetic offer extraction. "
                "Return strict JSON with keys selected_segments, excluded_segments, and summary. "
                "Only choose segments that contain concrete service, membership, promotion, pricing, bundle, or date evidence. "
                "Exclude navigation, CTA, commerce, account, and general marketing copy."
            ),
        },
        {
            "role": "user",
            "content": (
                "Choose all useful evidence blocks for extracting structured offers. Do not cap selection when many rows are prices.\n"
                "Return JSON in this shape:\n"
                '{"selected_segments":[{"index":0,"reason":"contains Botox unit price"}],'
                '"excluded_segments":[{"index":3,"reason":"navigation or CTA"}],"summary":"..."}\n'
                f"Input:\n{json.dumps(user_payload, ensure_ascii=False, indent=2)}"
            ),
        },
    ]


def build_offer_extraction_messages(
    row: Dict[str, Any],
    selected_segments: List[Dict[str, Any]],
) -> List[Dict[str, str]]:
    segment_lines = [f"[{item['index']}] {item['text']}" for item in selected_segments]
    schema_lines = ", ".join(OFFER_OUTPUT_FIELDS)
    standardized_names = json.dumps(get_standardized_service_names(), ensure_ascii=False)
    display_names = json.dumps(get_display_service_names()[:500], ensure_ascii=False)
    return [
        {
            "role": "system",
            "content": (
                "You extract aesthetic offers into strict JSON. "
                "Use only the supplied evidence segments. "
                "Do not infer missing values. "
                "Do not treat navigation, CTA, or commerce labels as service names. "
                "Do not extract free consultations, consultation-only bookings, membership plan tier fees, "
                "or retail skincare/catalog shop products as offers. "
                "Membership plan structure is stored separately; retail SKUs belong in promo_products_master. "
                "If a pricing block contains multiple offers, split them into separate records. "
                "service_name and canonical_service_name must be canonical categories from the allowed enum. "
                "display_service_name must preserve the exact visible treatment/product name from the page. "
                "Return JSON with a single top-level key offers."
            ),
        },
        {
            "role": "user",
            "content": (
                f"Extract offers for {row.get('domain_name', '')} {row.get('subpage_url', '')}.\n"
                f"Each offer must include these keys: {schema_lines}.\n"
                f"Allowed canonical service_name/canonical_service_name enum: {standardized_names}.\n"
                f"Known display service names/aliases, when applicable: {display_names}.\n"
                "For service_name use the best canonical enum value. For display_service_name use the visible page wording such as Restylane Kysse or Lip Filler. "
                "For canonical_service_name use the same canonical enum as service_name. "
                "For missing scalar fields use an empty string. For evidence_segments use a list of segment indexes.\n"
                f"Evidence:\n{json.dumps(segment_lines, ensure_ascii=False, indent=2)}"
            ),
        },
    ]


def rule_based_candidate_block_selection(segments: List[Dict[str, Any]], max_segments: int = 0) -> Dict[str, Any]:
    ranked = sorted(segments, key=lambda item: (-int(item.get("score", 0)), item.get("index", 0)))
    kept = [item for item in ranked if item.get("score", 0) > 0]
    if max_segments and max_segments > 0:
        kept = kept[:max_segments]
    selected = [{"index": item["index"], "reason": "high heuristic relevance"} for item in kept]
    selected_ids = {item["index"] for item in kept}
    excluded = [{"index": item["index"], "reason": "not selected by heuristic"} for item in ranked if item.get("index") not in selected_ids]
    return {
        "selected_segments": selected,
        "excluded_segments": excluded,
        "summary": "Heuristic fallback used.",
    }


def normalize_offer_record(record: Dict[str, Any], allowed_indexes: set[int]) -> Dict[str, Any]:
    normalized = {field: "" for field in OFFER_OUTPUT_FIELDS}
    for field in OFFER_OUTPUT_FIELDS:
        if field == "evidence_segments":
            continue
        value = record.get(field, "")
        if value is None:
            value = ""
        if isinstance(value, (dict, list)):
            normalized[field] = json.dumps(value, ensure_ascii=False, separators=(",", ":"))
        else:
            normalized[field] = str(value).strip()

    evidence = record.get("evidence_segments", [])
    if not isinstance(evidence, list):
        evidence = [evidence] if evidence != "" else []
    normalized["evidence_segments"] = [int(item) for item in evidence if str(item).isdigit() and int(item) in allowed_indexes]
    normalize_service_identity(normalized)
    category, _, confidence = resolve_service_category(
        normalized.get("service_name", ""),
        normalized.get("service_category", ""),
        min_confidence="medium",
    )
    if category and confidence in {"high", "medium"}:
        normalized["service_category"] = category
    return normalized


def normalize_offer_payload(payload: Any, allowed_indexes: set[int]) -> Dict[str, Any]:
    data = parse_json_payload(payload, {})
    offers = data.get("offers", []) if isinstance(data, dict) else []
    if not isinstance(offers, list):
        offers = []
    return {
        "offers": filter_service_offers(
            [
                normalize_offer_record(item, allowed_indexes)
                for item in offers
                if isinstance(item, dict)
            ]
        )
    }


@dataclass
class OpenAICompatibleClient:
    api_url: str
    api_key: str
    model: str
    timeout: int = 90

    def create_json_response(
        self,
        messages: List[Dict[str, str]],
        *,
        json_schema: Optional[Dict[str, Any]] = None,
    ) -> Dict[str, Any]:
        # ponytail: reasoning 模型（gpt-5*/o1*/o3*）不支持 temperature=0，只允许默认 1。
        body: Dict[str, Any] = {
            "model": self.model,
            "messages": messages,
        }
        if json_schema:
            body["response_format"] = {
                "type": "json_schema",
                "json_schema": {
                    "name": "structured_output",
                    "strict": True,
                    "schema": _schema_for_gemini(json_schema),
                },
            }
        else:
            body["response_format"] = {"type": "json_object"}
        if not self._is_reasoning_model():
            body["temperature"] = 0
        response = requests.post(
            self.api_url,
            headers={
                "Authorization": f"Bearer {self.api_key}",
                "Content-Type": "application/json",
            },
            json=body,
            timeout=self.timeout,
        )
        response.raise_for_status()
        payload = response.json()
        choices = payload.get("choices", [])
        if not choices:
            raise RuntimeError("LLM response missing choices")
        message = choices[0].get("message", {})
        content = message.get("content", "")
        if isinstance(content, list):
            content = "".join(item.get("text", "") for item in content if isinstance(item, dict))
        return parse_json_payload(content, {})

    def _is_reasoning_model(self) -> bool:
        name = (self.model or "").lower()
        return name.startswith(("gpt-5", "o1", "o3", "o4"))


def _schema_for_gemini(schema: Dict[str, Any]) -> Dict[str, Any]:
    """Drop JSON Schema meta keys Gemini does not need."""
    return {k: v for k, v in schema.items() if k not in ("$schema", "title", "description")}


def _messages_to_gemini(messages: List[Dict[str, str]]) -> tuple[Optional[Dict[str, Any]], List[Dict[str, Any]]]:
    system_parts: List[str] = []
    contents: List[Dict[str, Any]] = []
    for message in messages:
        role = str(message.get("role", "user")).lower()
        text = str(message.get("content", ""))
        if role == "system":
            system_parts.append(text)
            continue
        gemini_role = "model" if role == "assistant" else "user"
        contents.append({"role": gemini_role, "parts": [{"text": text}]})
    system_instruction = {"parts": [{"text": "\n\n".join(system_parts)}]} if system_parts else None
    return system_instruction, contents


@dataclass
class GeminiNativeClient:
    api_key: str
    model: str
    timeout: int = 90
    base_url: str = "https://generativelanguage.googleapis.com/v1beta"

    def create_json_response(
        self,
        messages: List[Dict[str, str]],
        *,
        json_schema: Optional[Dict[str, Any]] = None,
    ) -> Dict[str, Any]:
        system_instruction, contents = _messages_to_gemini(messages)
        generation_config: Dict[str, Any] = {"responseMimeType": "application/json"}
        if json_schema:
            generation_config["responseJsonSchema"] = _schema_for_gemini(json_schema)
        if not self._is_reasoning_model():
            generation_config["temperature"] = 0

        body: Dict[str, Any] = {"contents": contents, "generationConfig": generation_config}
        if system_instruction:
            body["systemInstruction"] = system_instruction

        model_path = self.model if self.model.startswith("models/") else f"models/{self.model}"
        url = f"{self.base_url}/{model_path}:generateContent"
        response = requests.post(
            url,
            headers={
                "Content-Type": "application/json",
                "x-goog-api-key": self.api_key,
            },
            json=body,
            timeout=self.timeout,
        )
        response.raise_for_status()
        payload = response.json()
        candidates = payload.get("candidates") or []
        if not candidates:
            raise RuntimeError("Gemini response missing candidates")
        parts = (candidates[0].get("content") or {}).get("parts") or []
        text = "".join(str(part.get("text", "")) for part in parts if isinstance(part, dict))
        if not text.strip():
            raise RuntimeError("Gemini response missing text")
        return parse_json_payload(text, {})

    def _is_reasoning_model(self) -> bool:
        name = (self.model or "").lower()
        return name.startswith(("gpt-5", "o1", "o3", "o4"))


def build_llm_ready_row(row: Dict[str, Any]) -> Dict[str, Any]:
    filtered_segments = load_filtered_segments(row)
    if not filtered_segments:
        filtered_segments, flags = filter_page_segments(build_text_segments(row.get("page_content", "")))
    else:
        flags = parse_json_payload(row.get("content_quality_flags"), [])
    llm_content = row.get("page_content_llm") or build_llm_ready_content(filtered_segments)
    return {
        **row,
        "page_segments_filtered": filtered_segments,
        "page_content_llm": llm_content,
        "content_quality_flags": flags if isinstance(flags, list) else [],
    }


def extract_offers_for_row(
    row: Dict[str, Any],
    client: Optional[StructuredLLMClient] = None,
    selection_limit: int = 0,
    extraction_chunk_size: int = 12,
) -> Dict[str, Any]:
    prepared = build_llm_ready_row(row)
    filtered_segments = prepared["page_segments_filtered"]
    candidate_messages = build_candidate_block_selection_messages(prepared, filtered_segments)
    if client:
        selection_payload = client.create_json_response(candidate_messages)
    else:
        selection_payload = rule_based_candidate_block_selection(filtered_segments, max_segments=selection_limit)

    selected_indexes = {
        int(item["index"])
        for item in selection_payload.get("selected_segments", [])
        if isinstance(item, dict) and str(item.get("index", "")).isdigit()
    }
    rule_selection = rule_based_candidate_block_selection(filtered_segments, max_segments=selection_limit)
    rule_indexes = {
        int(item["index"])
        for item in rule_selection.get("selected_segments", [])
        if isinstance(item, dict) and str(item.get("index", "")).isdigit()
    }
    # Use the union so model selection cannot silently drop price rows from long tables.
    selected_indexes |= rule_indexes
    selection_payload = {
        **selection_payload,
        "selected_segments": [
            {"index": index, "reason": "llm_or_rule_selected"}
            for index in sorted(selected_indexes)
        ],
        "rule_selected_count": len(rule_indexes),
        "llm_selected_count": len(selection_payload.get("selected_segments", []) or []),
        "selection_strategy": "llm_union_rule_high_signal",
    }

    selected_segments = [item for item in filtered_segments if item.get("index") in selected_indexes]
    promotion_messages = build_promotion_extraction_messages(prepared, selected_segments)
    promotion_schema = load_promotion_extraction_schema()
    chunk_payloads: List[Dict[str, Any]] = []
    if client and selected_segments:
        for segment_chunk in chunk_segments(selected_segments, extraction_chunk_size):
            chunk_payloads.append(
                client.create_json_response(
                    build_promotion_extraction_messages(prepared, segment_chunk),
                    json_schema=promotion_schema,
                )
            )
        promotion_payload = merge_promotion_payloads(chunk_payloads)
    else:
        promotion_payload = {"promotion": {}}

    normalized_offers = promotion_payload_to_offers(promotion_payload, allowed_indexes=selected_indexes)
    membership_plans: List[Dict[str, Any]] = []
    if client and is_membership_page_url(str(prepared.get("subpage_url") or "")):
        page_text = prepared.get("page_content_llm") or prepared.get("page_content") or ""
        membership_plans = extract_membership_plans_for_row(
            prepared,
            client=client,
            page_content=page_text,
        )

    return {
        "domain_name": prepared.get("domain_name", ""),
        "subpage_url": prepared.get("subpage_url", ""),
        "content_quality_flags": prepared.get("content_quality_flags", []),
        "candidate_block_selection": selection_payload,
        "selected_segments": selected_segments,
        "candidate_block_selection_prompt": candidate_messages,
        "offer_extraction_prompt": promotion_messages,
        "offer_extraction_chunks": len(chunk_payloads),
        "promotion_extraction": promotion_payload,
        "offers": normalized_offers,
        "membership_plans": normalize_membership_plan_refs(membership_plans),
    }


def build_client_from_env(
    *,
    api_url: Optional[str] = None,
    api_key: Optional[str] = None,
    model: Optional[str] = None,
    api_key_env: str = "LLM_API_KEY",
) -> Optional[StructuredLLMClient]:
    resolved_model = (model or os.getenv("LLM_MODEL", "")).strip()
    resolved_key = (api_key or os.getenv(api_key_env, "")).strip().strip("'\"")
    backend = os.getenv("LLM_BACKEND", "").strip().lower()
    use_gemini = backend in {"gemini", "gemini_native"} or (
        not backend and "gemini" in resolved_model.lower()
    )
    if use_gemini and resolved_key and resolved_model:
        native = build_gemini_client_from_env(api_key=resolved_key, model=resolved_model)
        if native:
            return native

    resolved_url = api_url or os.getenv("LLM_API_URL", "").strip()
    if not (resolved_key and resolved_url and resolved_model):
        return None
    return OpenAICompatibleClient(api_url=resolved_url, api_key=resolved_key, model=resolved_model)


def build_gemini_client_from_env(
    *,
    api_key: Optional[str] = None,
    model: Optional[str] = None,
) -> Optional[GeminiNativeClient]:
    resolved_key = (api_key or os.getenv("LLM_API_KEY") or os.getenv("GEMINI_API_KEY", "")).strip().strip("'\"")
    resolved_model = (model or os.getenv("LLM_MODEL", "")).strip()
    if not (resolved_key and resolved_model):
        return None
    return GeminiNativeClient(api_key=resolved_key, model=resolved_model)
