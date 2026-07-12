from datetime import date, timedelta

from utils.social_ingestion import (
    chunked,
    local_day_bounds_utc,
    local_date_window_bounds_utc,
    resolve_target_date,
    stringify_timestamp,
)


def test_social_helpers_preserve_date_and_chunk_boundaries():
    assert resolve_target_date("2026-07-12", "Asia/Shanghai") == date(2026, 7, 12)
    assert list(chunked(["a", "b", "c"], 2)) == [["a", "b"], ["c"]]
    assert list(chunked(["a"], 0)) == [["a"]]


def test_social_helpers_calculate_timezone_aware_utc_windows():
    start, end = local_day_bounds_utc(date(2026, 7, 12), "Asia/Shanghai")
    assert start.isoformat() == "2026-07-11T16:00:00+00:00"
    assert end.isoformat() == "2026-07-12T16:00:00+00:00"

    window_start, window_end = local_date_window_bounds_utc(
        date(2026, 7, 10), date(2026, 7, 12), "Asia/Shanghai"
    )
    assert window_start == start - timedelta(days=2)
    assert window_end == end


def test_stringify_timestamp_is_stable_for_none_and_non_strings():
    assert stringify_timestamp(None) == ""
    assert stringify_timestamp(" 2026-07-12 ") == "2026-07-12"
    assert stringify_timestamp(123) == "123"
