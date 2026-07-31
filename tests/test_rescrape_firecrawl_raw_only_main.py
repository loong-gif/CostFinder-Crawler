from scripts.rescrape_firecrawl_raw_only_main import (
    build_success_payload,
    build_v1_scrape_payload,
    group_target_rows,
    record_failure,
)


def test_v1_scrape_payload_enables_only_main_content_without_block_ads() -> None:
    payload = build_v1_scrape_payload("https://example.com/specials")

    assert payload == {
        "url": "https://example.com/specials",
        "formats": ["markdown", "links"],
        "onlyMainContent": True,
    }


def test_group_target_rows_deduplicates_urls_and_keeps_all_raw_ids() -> None:
    groups = group_target_rows(
        [
            {"id": 1, "source_url": "https://www.example.com/specials/"},
            {"id": 2, "source_url": "https://example.com/specials"},
            {"id": 3, "source_url": "https://example.com/services"},
            {"id": 4, "source_url": "https://example.com/vein-specialist"},
        ]
    )

    assert groups == {"https://example.com/specials": [1, 2]}


def test_success_payload_clears_legacy_full_content_fields() -> None:
    payload = build_success_payload(
        {"success": True, "data": {"markdown": "# Special\n\n$99", "links": []}}
    )

    assert payload["markdown"] == "# Special\n\n$99"
    assert payload["response_json"]["success"] is True
    assert payload["html"] is None
    assert payload["raw_html"] is None
    assert payload["screenshot"] is None
    assert payload["images"] is None


def test_record_failure_does_not_downgrade_existing_good_response() -> None:
    class Client:
        def __init__(self) -> None:
            self.calls = []

        def update_row(self, *args):
            self.calls.append(args)

    client = Client()
    updated = record_failure(
        client,
        [
            {"id": 1, "success": True, "response_json": {"success": True}},
            {"id": 2, "success": False, "response_json": None},
        ],
        "timeout",
    )

    assert updated == 1
    assert client.calls[0][1] == {"id": "eq.2"}
