"""
LLM-driven offer extraction helpers.
"""
from __future__ import annotations

import json
import os
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, List, Optional, Protocol

import requests

from crawler.promo_site_crawler import normalize_segment_text
from utils.offer_scope_filter import filter_service_offers
from utils.service_category_lookup import resolve_service_category

SERVICE_NAME_DICT_PATH = Path(__file__).resolve().parents[1] / "service_name_dict.json"

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

    # Longer keys first so "botox" wins over "tox", "lip filler" over "filler".
    substring_keys = sorted(
        [(alias, canonical) for alias, canonical in alias_by_normalized.items()]
        + [(key, name) for key, name in canonical_by_normalized.items()],
        key=lambda pair: len(pair[0]),
        reverse=True,
    )
    for text in normalized_values:
        for key, canonical in substring_keys:
            if re.search(rf"(?<![a-z0-9]){re.escape(key)}(?![a-z0-9])", text):
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


class StructuredLLMClient(Protocol):
    model: str

    def create_json_response(
        self,
        messages: List[Dict[str, str]],
        *,
        json_schema: Optional[Dict[str, Any]] = None,
    ) -> Dict[str, Any]: ...


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


if __name__ == "__main__":
    assert canonicalize_service_name("Botox", "botox cosmetic") == "Botox"
    offers = promotion_payload_to_offers(
        {"promotion": {"promotion_title": "Sale", "offers": [{"price_model": "from", "discount_price": 8, "items": [{"item_name": "Jeuveau"}]}]}},
        allowed_indexes=set(),
    )
    assert offers and offers[0]["display_service_name"] == "Jeuveau"
