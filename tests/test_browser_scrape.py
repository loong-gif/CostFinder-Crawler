from __future__ import annotations

import pytest

from utils.browser_scrape import BrowserScrapeFailed, ego_scrape, html_to_markdown, is_block_page


def test_ego_script_error_on_a_page_is_that_page_failure(tmp_path, monkeypatch):
    # PATH 上放一个失败的 ego-browser：脚本在页面上出错时 crawl 记录该页面失败并继续其他页面
    fake = tmp_path / "ego-browser"
    fake.write_text("#!/bin/sh\necho 'Error: page crashed' >&2\nexit 1\n")
    fake.chmod(0o755)
    monkeypatch.setenv("PATH", f"{tmp_path}:/usr/bin:/bin")
    with pytest.raises(BrowserScrapeFailed, match="ego_script_failed: Error: page crashed"):
        ego_scrape("https://glow.example/specials")


def test_block_page_served_with_200_is_detected():
    assert is_block_page("# 402\n\n### Access Denied !\n\nYou are not authorized person to access this site.")
    assert is_block_page("## Just a moment...\n\nChecking your browser")


def test_promotion_page_is_not_block_page():
    assert not is_block_page("# Monthly Specials\n\n## Botox $10/unit\n\nValid through November.")


def test_html_to_markdown_drops_scripts_and_keeps_text():
    html = "<html><head><style>p{}</style></head><body><script>x()</script><h2>Specials</h2><p>Botox $10</p></body></html>"
    assert html_to_markdown(html) == "## Specials\n\nBotox $10"
