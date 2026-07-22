"""Tests for firecrawl_scrape_raw_db helpers."""
from utils.firecrawl_scrape_raw_db import (
    canonical_scrape_url,
    scrape_request_fingerprint,
    scrape_response_to_row_fields,
)


def test_canonical_scrape_url_strips_www_and_trailing_slash() -> None:
    assert (
        canonical_scrape_url("https://www.example.com/pricing/")
        == "https://example.com/pricing"
    )


def test_scrape_fingerprint_stable_for_same_url() -> None:
    a = scrape_request_fingerprint("https://www.example.com/a/")
    b = scrape_request_fingerprint("https://example.com/a")
    assert a == b


def test_scrape_fingerprint_differs_when_formats_change() -> None:
    a = scrape_request_fingerprint("https://example.com/a", formats=["markdown"])
    b = scrape_request_fingerprint("https://example.com/a", formats=["markdown", "html"])
    assert a != b


def test_scrape_response_to_row_fields_api_envelope() -> None:
    row = scrape_response_to_row_fields(
        {
            "success": True,
            "id": "job-1",
            "creditsUsed": 2,
            "data": {
                "markdown": "# hi",
                "links": ["https://example.com/a"],
                "metadata": {"title": "Hi", "statusCode": 200},
            },
        }
    )
    assert row["markdown"] == "# hi"
    assert row["links"] == ["https://example.com/a"]
    assert row["metadata"]["title"] == "Hi"
    assert row["scrape_job_id"] == "job-1"
    assert row["credits_used"] == 2


def test_scrape_response_to_row_fields_cli_body() -> None:
    row = scrape_response_to_row_fields(
        {
            "markdown": "body",
            "links": [],
            "metadata": {"sourceURL": "https://example.com/"},
        }
    )
    assert row["markdown"] == "body"
    assert row["metadata"]["sourceURL"] == "https://example.com/"
