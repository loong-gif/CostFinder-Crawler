"""Gemini client factory shared by the promotion backfill and audit scripts."""
from __future__ import annotations

import os

from utils.offer_extraction_llm import GeminiNativeClient

GEMINI_MODEL = "gemini-3.5-flash-lite"


def build_gemini_client(environ: dict[str, str] | None = None) -> GeminiNativeClient:
    env = environ if environ is not None else os.environ
    api_key = str(env.get("GEMINI_API_KEY") or "").strip().strip("'\"")
    if not api_key:
        raise RuntimeError("Missing GEMINI_API_KEY")
    return GeminiNativeClient(api_key=api_key, model=GEMINI_MODEL)
