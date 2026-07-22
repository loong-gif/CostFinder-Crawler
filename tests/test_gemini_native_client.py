"""Gemini native structured-output client self-check."""
from unittest.mock import MagicMock, patch

from utils.offer_extraction_llm import GeminiNativeClient, _schema_for_gemini


def test_schema_for_gemini_strips_meta_keys():
    schema = {"$schema": "x", "title": "T", "type": "object", "properties": {"a": {"type": "string"}}}
    assert "$schema" not in _schema_for_gemini(schema)
    assert "type" in _schema_for_gemini(schema)


def test_create_json_response_sends_response_json_schema():
    client = GeminiNativeClient(api_key="test-key", model="gemini-3.1-flash-lite")
    schema = {"type": "object", "properties": {"services": {"type": "array"}}, "required": ["services"]}
    fake_resp = MagicMock()
    fake_resp.json.return_value = {
        "candidates": [{"content": {"parts": [{"text": '{"services": []}'}]}}],
    }
    fake_resp.raise_for_status = MagicMock()

    with patch("utils.offer_extraction_llm.requests.post", return_value=fake_resp) as post:
        result = client.create_json_response(
            [{"role": "system", "content": "sys"}, {"role": "user", "content": "user"}],
            json_schema=schema,
        )

    assert result == {"services": []}
    body = post.call_args.kwargs["json"]
    assert body["generationConfig"]["responseMimeType"] == "application/json"
    assert body["generationConfig"]["responseJsonSchema"]["type"] == "object"
    assert post.call_args.kwargs["headers"]["x-goog-api-key"] == "test-key"
    assert "params" not in post.call_args.kwargs
    assert "key=" not in post.call_args.args[0]


def test_openai_client_uses_json_schema_when_provided():
    from utils.offer_extraction_llm import OpenAICompatibleClient

    client = OpenAICompatibleClient(
        api_url="https://example.com/v1/chat/completions",
        api_key="k",
        model="gpt-4o-mini",
    )
    schema = {"type": "object", "properties": {"ok": {"type": "boolean"}}, "required": ["ok"]}
    fake_resp = MagicMock()
    fake_resp.json.return_value = {"choices": [{"message": {"content": '{"ok": true}'}}]}
    fake_resp.raise_for_status = MagicMock()

    with patch("utils.offer_extraction_llm.requests.post", return_value=fake_resp) as post:
        result = client.create_json_response([{"role": "user", "content": "x"}], json_schema=schema)

    assert result == {"ok": True}
    body = post.call_args.kwargs["json"]
    assert body["response_format"]["type"] == "json_schema"
    assert body["response_format"]["json_schema"]["schema"]["type"] == "object"
