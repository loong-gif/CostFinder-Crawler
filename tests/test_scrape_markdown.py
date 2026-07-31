from utils.scrape_markdown import denoise_scrape_markdown, prepare_scrape_markdown, truncate_scrape_markdown


def test_denoise_removes_growth99_and_tracking_debug() -> None:
    raw = (
        "# Membership\n\n$295/month\n\n"
        "Growth99+\n\nLead Capture Form\n\n"
        "## Tracking Debug\n\nN/A\n"
    )
    out = denoise_scrape_markdown(raw)
    assert "$295/month" in out
    assert "Growth99" not in out
    assert "Tracking Debug" not in out


def test_denoise_trims_leading_skip_link() -> None:
    raw = "[Skip to content](https://x.com)\n\n- phone\n\n# Pricing\n\nBotox $12/unit"
    out = denoise_scrape_markdown(raw)
    assert out.startswith("# Pricing")
    assert "Skip to content" not in out


def test_truncate_adds_marker() -> None:
    out = truncate_scrape_markdown("a\n" * 20, max_chars=10)
    assert "truncated" in out


def test_denoise_removes_cookie_and_subscribe_noise_without_removing_price() -> None:
    raw = (
        "# Summer Special\n\nBotox $10/unit through August 31.\n\n"
        "## Cookie Preferences\n\nManage Cookie Settings\n\n"
        "## Subscribe to Our Newsletter\n\nEnter your email address\n\n"
        "## Footer\n\nCopyright 2026 Example"
    )

    out = denoise_scrape_markdown(raw)

    assert "Botox $10/unit" in out
    assert "Cookie Preferences" not in out
    assert "Subscribe to Our Newsletter" not in out
    assert "Copyright 2026" not in out
