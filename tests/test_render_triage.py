from __future__ import annotations

import pytest

from utils.render_triage import fragment_reason, triage_entry, triage_plan

PAGE = ("Specials\n\nNew Client $9 Per Unit Botox offer. New clients can get Botox at our everyday membership "
        "pricing on their first visit. Minimum session $180.\n\n$50 off your first Hydrafacial.")


@pytest.mark.parametrize("line", ["| --- | --- |", "=======================", "/per", "-50%", "10% OFF", "SAVE $339!", "$3,650"])
def test_fragments_are_detected(line):
    assert fragment_reason(line)


@pytest.mark.parametrize("line", ["$12 Botox", "Botox $9/unit", "New Client $50 Off Your First Hydrafacial Offer"])
def test_complete_statements_are_kept(line):
    assert fragment_reason(line) is None


def entry(content, url="https://a.com/specials", **extra):
    return {"kind": "promotion", "action": "insert", "source_url": url, "content": content, **extra}


def test_traceable_complete_entry_needs_no_render():
    result = triage_entry(entry(["New Client $9 Per Unit Botox offer", "$50 off your first Hydrafacial."]), PAGE)
    assert result["route"] == "markdown_traceable"


def test_line_missing_from_page_text_goes_to_render():
    result = triage_entry(entry(["Chin Filler now $299 (was $499); 40% off"]), PAGE)
    assert result["route"] == "render"
    assert result["reasons"] == ["line_not_in_page_text"]


def test_missing_page_text_goes_to_render():
    assert triage_entry(entry(["$50 off your first Hydrafacial."]), "")["reasons"] == ["no_page_text"]


def test_source_card_not_extracted_goes_to_render():
    reviews = [{"source_cards_not_extracted": ["Lip Filler Special"]}]
    result = triage_entry(entry(["$50 off your first Hydrafacial."], source_reviews=reviews), PAGE)
    assert result["route"] == "render"
    assert result["source_cards_not_extracted"] == ["Lip Filler Special"]


def test_link_residue_and_oversize_and_price_list_are_rejected_without_render():
    assert triage_entry(entry(["[20units Botox $200](https://book.example.com/x)"]), PAGE)["reasons"][0] == "link_residue"
    assert "oversize_page_list" in triage_entry(entry([f"Botox special number {i} $10 off" for i in range(16)]), PAGE)["reasons"]
    assert "price_list_url" in triage_entry(entry(["$50 off your first Hydrafacial."], url="https://a.com/price-list"), PAGE)["reasons"]


def test_fragments_are_removed_and_empty_remainder_is_rejected():
    kept = triage_entry(entry(["| --- | --- |", "$50 off your first Hydrafacial."]), PAGE)
    assert kept["clean_content"] == ["$50 off your first Hydrafacial."] and kept["route"] == "markdown_traceable"
    assert triage_entry(entry(["-50%", "10% OFF"]), PAGE)["reasons"] == ["no_content_after_fragment_removal"]


def test_plan_triage_lists_pages_to_render_and_skips_rejected_and_memberships():
    plan = {"entries": [
        entry(["$50 off your first Hydrafacial."]),
        entry(["Chin Filler now $299 (was $499); 40% off"], url="https://b.com/specials"),
        {**entry(["x"]), "action": "rejected"},
        {"kind": "membership", "action": "insert", "source_url": "https://c.com/membership"}]}
    scrapes = {"a.com/specials": {"markdown": PAGE}, "b.com/specials": {"markdown": PAGE}}
    triage = triage_plan(plan, scrapes)
    assert triage["summary"] == {"markdown_traceable": 1, "render": 1}
    assert triage["render_pages"] == ["https://b.com/specials"]
