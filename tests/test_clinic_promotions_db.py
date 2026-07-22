"""Unit tests for clinic_promotions_db helpers."""
from utils.clinic_promotions_db import _title_from_url, _norm_url


def test_norm_url_strips_trailing_slash() -> None:
    assert _norm_url("https://example.com/pricing/") == "https://example.com/pricing"


def test_title_from_url_uses_path_segment() -> None:
    assert _title_from_url("https://clinic.com/summer-specials") == "Summer Specials"
