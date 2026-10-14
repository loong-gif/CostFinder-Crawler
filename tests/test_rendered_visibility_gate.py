from utils.rendered_visibility_gate import (
    filter_visible_membership_benefits,
    filter_visible_promotion_content,
)


def _evidence(*claims):
    return {
        "status": "page_accessible",
        "claimResults": [
            {"recordType": "promotion", "claim": claim, "status": "visible_match"}
            for claim in claims
        ],
    }


def test_requires_rendered_visibility_evidence():
    result = filter_visible_promotion_content(["Save $20 on Botox."], None)
    assert result.accepted is False
    assert result.reason == "rendered_visibility_required"


def test_filters_dom_only_and_missing_claims_before_write():
    result = filter_visible_promotion_content(
        ["Save $20 on Botox.", "$12 per unit"],
        _evidence("Save $20 on Botox."),
    )
    assert result.accepted is True
    assert result.content == ("Save $20 on Botox.",)
    assert result.removed == ("$12 per unit",)


def test_rejects_page_without_visible_promotion_content():
    result = filter_visible_promotion_content(
        ["Save $20 on Botox."],
        {"status": "page_accessible", "claimResults": []},
    )
    assert result.accepted is False
    assert result.reason == "no_rendered_visible_promotion_content"


def test_membership_benefits_require_membership_visibility_evidence():
    result = filter_visible_membership_benefits(
        ["15% off every service", "Priority booking"],
        {
            "status": "page_accessible",
            "claimResults": [
                {
                    "recordType": "membership",
                    "claim": "15% off every service",
                    "status": "visible_match",
                },
                {
                    "recordType": "promotion",
                    "claim": "Priority booking",
                    "status": "visible_match",
                },
            ],
        },
    )
    assert result.accepted is True
    assert result.content == ("15% off every service",)
    assert result.removed == ("Priority booking",)


def test_membership_benefits_have_membership_specific_empty_reason():
    result = filter_visible_membership_benefits(
        ["Priority booking"],
        {"status": "page_accessible", "claimResults": []},
    )
    assert result.accepted is False
    assert result.reason == "no_rendered_visible_membership_content"


def test_membership_image_only_label_alone_cannot_pass_visibility_gate():
    result = filter_visible_membership_benefits(
        ["One facial per month"],
        {
            "status": "page_accessible",
            "claimResults": [{
                "recordType": "membership",
                "claim": "One facial per month",
                "status": "image_only",
                "visibleEvidence": {"kind": "rendered_image_ocr", "imageUrl": "https://example.test/card.png"},
            }],
        },
    )
    assert result.accepted is False
    assert result.content == ()
