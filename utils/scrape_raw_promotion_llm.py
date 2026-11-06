from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Mapping, Optional

import requests

from utils.offer_extraction_llm import OpenAICompatibleClient, parse_json_payload


def resolve_local_llm_api_key(environ: Mapping[str, str]) -> str:
    """Use only a local-model credential; never forward a cloud LLM secret."""
    return str(environ.get("LOCAL_LLM_API_KEY") or "local-model").strip()


def normalize_llm_api_url(base_url: str) -> str:
    base = str(base_url or "").strip().rstrip("/")
    if not base:
        raise ValueError("llm_base_url is required")
    return base if base.endswith("/chat/completions") else f"{base}/chat/completions"


@dataclass
class ProxySafeOpenAICompatibleClient(OpenAICompatibleClient):
    """OpenAI-compatible client that never inherits shell proxy settings."""

    session: requests.Session = field(init=False, repr=False)

    def __post_init__(self) -> None:
        self.session = requests.Session()
        self.session.trust_env = False

    def create_json_response(
        self,
        messages: list[dict[str, str]],
        *,
        json_schema: Optional[dict[str, Any]] = None,
    ) -> dict[str, Any]:
        body: dict[str, Any] = {"model": self.model, "messages": messages, "temperature": 0}
        if json_schema:
            body["response_format"] = {
                "type": "json_schema",
                "json_schema": {
                    "name": "promotion_extraction",
                    "strict": True,
                    "schema": {
                        key: value
                        for key, value in json_schema.items()
                        if key not in {"$schema", "title", "description"}
                    },
                },
            }
        else:
            body["response_format"] = {"type": "json_object"}
        response = self.session.post(
            self.api_url,
            headers={
                "Authorization": f"Bearer {self.api_key}",
                "Content-Type": "application/json",
            },
            json=body,
            timeout=self.timeout,
        )
        try:
            response.raise_for_status()
        except requests.HTTPError as exc:
            detail = str(getattr(response, "text", "") or "").strip()
            status_code = getattr(response, "status_code", "unknown")
            raise RuntimeError(
                f"LLM request failed: status={status_code}; response={detail[:1000]}"
            ) from exc
        choices = response.json().get("choices") or []
        if not choices:
            raise RuntimeError("LLM response missing choices")
        content = (choices[0].get("message") or {}).get("content", "")
        if isinstance(content, list):
            content = "".join(
                str(part.get("text") or "") for part in content if isinstance(part, dict)
            )
        payload = parse_json_payload(content, {})
        if not isinstance(payload, dict):
            raise ValueError("LLM returned a non-object payload")
        return payload
