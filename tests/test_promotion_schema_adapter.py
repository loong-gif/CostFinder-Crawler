"""Promotion schema adapter self-check."""
from utils.offer_extraction_llm import promotion_payload_to_offers


def test_promotion_payload_to_offers_maps_items_and_prices():
    payload = {
        "promotion": {
            "promotion_title": "Summer Sale",
            "offers": [
                {
                    "price_model": "from",
                    "discount_price": 8,
                    "items": [{"item_name": "Jeuveau", "unit_type": "unit"}],
                }
            ],
        }
    }
    offers = promotion_payload_to_offers(payload, allowed_indexes={0, 1})
    assert len(offers) == 1
    assert offers[0]["display_service_name"] == "Jeuveau"
    assert offers[0]["discount_price"] == "8"
    assert offers[0]["unit_type"] == "unit"
    assert offers[0]["template_type"] == "FROM_PRICE"
    assert offers[0]["offer_content"] == "Summer Sale"


def test_build_client_from_env_prefers_gemini_for_gemini_model(monkeypatch):
    monkeypatch.setenv("LLM_MODEL", "gemini-3.1-flash-lite")
    monkeypatch.setenv("LLM_API_KEY", "test-key")
    monkeypatch.delenv("LLM_BACKEND", raising=False)

    from utils.offer_extraction_llm import GeminiNativeClient, build_client_from_env

    client = build_client_from_env()
    assert isinstance(client, GeminiNativeClient)
    assert client.model == "gemini-3.1-flash-lite"
