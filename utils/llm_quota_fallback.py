"""Gemini 额度用尽（429 重试用尽）时，抽取请求改由 CPA 上的 claude-haiku-5-5 完成。"""
from __future__ import annotations

import os

from utils.service_endpoints import CPA_URL

FALLBACK_MODEL = "claude-haiku-5-5"


class LLMQuotaExceeded(RuntimeError):
    pass


class QuotaFallbackClient:
    def __init__(self, primary, fallback):
        self.primary = primary
        self.fallback = fallback
        self.monthly_cap_exhausted = False

    def create_json_response(self, messages, *, json_schema=None):
        if self.monthly_cap_exhausted:
            return self.fallback.create_json_response(messages, json_schema=json_schema)
        try:
            return self.primary.create_json_response(messages, json_schema=json_schema)
        except LLMQuotaExceeded as exc:
            if "monthly spending cap" in str(exc).lower():
                self.monthly_cap_exhausted = True
                print("Gemini monthly spending cap reached; using CPA for this run", flush=True)
            return self.fallback.create_json_response(messages, json_schema=json_schema)

    def __getattr__(self, name):
        # 图片等其余方法只由主客户端提供，不走备用模型
        return getattr(self.primary, name)


def with_cpa_fallback(primary, client_class):
    """设置了 CPA_API_KEY 时给主客户端加上备用模型；未设置时原样返回。"""
    api_key = os.getenv("CPA_API_KEY", "").strip()
    if not api_key:
        return primary
    fallback = client_class(api_url=f"{CPA_URL}/v1/chat/completions", api_key=api_key, model=FALLBACK_MODEL)
    fallback.session.trust_env = False  # CPA 在监控主机上，本地 HTTP(S)_PROXY 会让它返回 502
    return QuotaFallbackClient(primary, fallback)
