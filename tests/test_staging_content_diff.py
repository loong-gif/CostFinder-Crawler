from utils.staging_content_diff import (
    classify_content_change,
    content_hash,
    has_price_signal,
    normalize_content,
)


def test_normalize_content_strips_segment_markers_and_whitespace():
    raw = "[SEGMENT 1] Botox   $199\n[SEGMENT 2]  Filler $599"
    assert normalize_content(raw) == "botox $199 filler $599"


def test_content_hash_is_stable_for_equivalent_text():
    a = "[SEGMENT 1] Botox $199"
    b = "botox   $199"
    assert content_hash(a) == content_hash(b)


def test_classify_unchanged_when_hashes_match():
    result = classify_content_change("Botox $199", "botox $199")
    assert result.change_type == "unchanged"


def test_classify_changed_when_price_removed():
    old = "Botox special $199 per unit"
    new = "Botox — call our office today"
    result = classify_content_change(old, new)
    assert result.change_type == "changed"
    assert result.price_signal_lost is True
    assert result.price_signal_gained is False


def test_has_price_signal_detects_dollar_amount():
    assert has_price_signal("Membership from $99/month") is True
    assert has_price_signal("About our clinic") is False
