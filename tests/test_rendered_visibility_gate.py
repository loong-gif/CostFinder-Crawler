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


def test_image_ocr_proof_must_name_its_visible_images(tmp_path):
    screenshot = tmp_path / "page.png"
    screenshot.write_bytes(b"png")
    claim = "One facial per month $99"
    proof = {"pageUrl": "https://a.example/m", "captured_at": "t", "transcript": claim,
             "screenshotPath": str(screenshot)}
    page = {"status": "page_accessible", "url": "https://a.example/m", "checked_at": "t",
            "visibleImages": [], "imageOcr": []}

    def gate(kind):
        return filter_visible_membership_benefits([claim], {**page, "claimResults": [
            {"recordType": "membership", "claim": claim, "status": "image_only",
             "visibleEvidence": {**proof, "kind": kind}}]})

    # 图片 OCR 证据没有列出可见图片时不能退回到截图校验
    assert gate("rendered_image_ocr").accepted is False
    assert gate("rendered_screenshot_ocr").accepted is True


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
