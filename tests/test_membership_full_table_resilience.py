from scripts.repair_clinic_memberships_full_table import parse_rate_limit_delay


def test_parse_rate_limit_delay_prefers_retry_after_header():
    assert parse_rate_limit_delay(
        'rate limit exceeded; retry after 7s',
        headers={"Retry-After": "12"},
        default=5.0,
    ) == 12.0


def test_parse_rate_limit_delay_reads_firecrawl_body():
    assert parse_rate_limit_delay(
        'Rate limit exceeded; please retry after 27s, resets at ...',
        headers={},
        default=5.0,
    ) == 27.0


def test_parse_rate_limit_delay_uses_bounded_default_for_unknown_error():
    assert parse_rate_limit_delay("rate limit exceeded", headers={}, default=5.0) == 5.0


def test_parse_rate_limit_delay_reads_gemini_retry_in_message():
    assert parse_rate_limit_delay("Please retry in 31.0s", headers={}, default=5.0) == 31.0
