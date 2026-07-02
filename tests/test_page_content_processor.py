import json

from utils.page_content_processor import infer_source_type, normalize_raw_page_item


def test_infer_source_type_prefers_explicit_markdown():
    item = {"source_type": "markdown", "page_content": "# Specials\nBotox $11/unit"}
    assert infer_source_type(item) == "markdown"


def test_normalize_raw_page_item_processes_markdown_into_staging_shape():
    item = {
        "subpage_url": "https://example.com/specials",
        "page_content": "# Specials\n\nBotox $11/unit limited time\nJuvederm $100 off",
        "domain": "example.com",
        "name": "Example Medspa",
        "source_type": "markdown",
    }

    row = normalize_raw_page_item(item, default_domain_name="example.com")

    assert row is not None
    assert row["subpage_url"] == "https://example.com/specials"
    assert row["domain_name"] == "example.com"
    assert row["name"] == "Example Medspa"
    assert row["page_content"]
    assert row["page_content_llm"]
    assert isinstance(json.loads(row["page_segments_raw"]), list)
    assert isinstance(json.loads(row["page_segments_filtered"]), list)
    assert isinstance(json.loads(row["content_quality_flags"]), list)
