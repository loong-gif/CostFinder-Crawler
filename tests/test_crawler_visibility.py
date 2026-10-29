from __future__ import annotations

from utils.crawler_visibility import assess_page, visible_text_from_html

FILLER = "Our medspa offers a full range of aesthetic services for every patient. " * 5


def _page(body: str) -> str:
    return f"<html><head><title>x</title></head><body><main>{FILLER}{body}</main></body></html>"


def test_visible_price_passes_without_browser() -> None:
    html = _page("<p>Botox $12 per unit this month</p>")
    result = assess_page("md", html, facts=["Botox $12 per unit"])
    assert not result.needs_browser and "Botox $12 per unit" in result.visible_text


def test_inline_hidden_aria_and_attribute_are_removed() -> None:
    html = _page(
        '<p style="display:none">Fake $1 deal</p><p aria-hidden="true">Fake $2 deal</p>'
        '<div hidden>Fake $3 deal</div><span class="sr-only">Fake $4 deal</span>'
        '<p style="visibility: hidden">Fake $5 deal</p><p style="opacity:0">Fake $6 deal</p>'
        "<template><p>Fake $7 deal</p></template><noscript>Fake $8 deal</noscript>"
        '<input type="hidden" value="Fake $9 deal"><p>Real $50 deal</p>'
    )
    text, _, _ = visible_text_from_html(html)
    assert "Real $50 deal" in text
    assert not any(f"Fake ${n} deal" in text for n in range(1, 10))


def test_closed_details_hides_body_but_keeps_summary() -> None:
    html = _page("<details><summary>Offers</summary><p>Hidden $99 offer</p></details>")
    text, _, _ = visible_text_from_html(html)
    assert "Offers" in text and "$99" not in text
    open_html = _page("<details open><summary>Offers</summary><p>Shown $99 offer</p></details>")
    assert "$99" in visible_text_from_html(open_html)[0]


def test_css_class_hiding_forces_browser_fallback() -> None:
    result = assess_page("md", _page('<div class="d-none">Maybe hidden $20</div>'))
    assert result.needs_browser and "css_class_hiding_possible" in result.reasons


def test_empty_thin_and_missing_html_fall_back() -> None:
    assert "empty_markdown" in assess_page("", _page("<p>x</p>")).reasons
    assert "no_html" in assess_page("md", None).reasons
    thin = assess_page("md", "<html><body><div id='root'></div></body></html>")
    assert thin.needs_browser and "thin_visible_text" in thin.reasons


def test_claim_missing_from_visible_text_falls_back() -> None:
    html = _page('<p style="display:none">Botox $12 per unit</p>')
    result = assess_page("md", html, facts=["Botox $12 per unit"])
    assert result.needs_browser and "claim_not_in_visible_text" in result.reasons


def test_promo_image_requires_browser() -> None:
    result = assess_page("md", _page('<img src="/img/fall-special-banner.jpg" alt="Fall special 20% off">'))
    assert result.needs_browser and "image_promo_candidate" in result.reasons
