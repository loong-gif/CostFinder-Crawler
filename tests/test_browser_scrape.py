from __future__ import annotations

from utils.browser_scrape import html_to_markdown, is_block_page


def test_block_page_served_with_200_is_detected():
    assert is_block_page("# 402\n\n### Access Denied !\n\nYou are not authorized person to access this site.")
    assert is_block_page("## Just a moment...\n\nChecking your browser")


def test_promotion_page_is_not_block_page():
    assert not is_block_page("# Monthly Specials\n\n## Botox $10/unit\n\nValid through November.")


def test_html_to_markdown_drops_scripts_and_keeps_text():
    html = "<html><head><style>p{}</style></head><body><script>x()</script><h2>Specials</h2><p>Botox $10</p></body></html>"
    assert html_to_markdown(html) == "## Specials\n\nBotox $10"
