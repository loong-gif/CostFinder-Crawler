from __future__ import annotations

from utils.crawler_visibility import assess_page

FILLER = "Our medspa offers a full range of aesthetic services for every patient. " * 5
PAGE_URL = "https://glow.example/specials"


def _page(body: str) -> str:
    return f"<html><head><title>x</title></head><body><main>{FILLER}{body}</main></body></html>"


def _assess(markdown: str, html, facts=()):
    return assess_page(markdown, html, facts, page_url=PAGE_URL, stylesheets={})


def test_visible_price_passes_without_browser() -> None:
    result = _assess("md", _page("<p>Botox $12 per unit this month</p>"), facts=["Botox $12 per unit"])
    assert not result.needs_browser and "Botox $12 per unit" in result.visible_text
    assert len(result.visible_digest) == 64


def test_inline_hidden_and_user_agent_hidden_elements_are_removed() -> None:
    html = _page(
        '<p style="display:none">Fake $1 deal</p><dialog>Fake $2 deal</dialog>'
        '<div hidden>Fake $3 deal</div><p style="font-size:0">Fake $4 deal</p>'
        '<p style="visibility: hidden">Fake $5 deal</p><p style="opacity:0">Fake $6 deal</p>'
        "<template><p>Fake $7 deal</p></template><noscript>Fake $8 deal</noscript>"
        '<input type="hidden" value="Fake $9 deal"><script>var a = "Fake $10 deal";</script>'
        '<select><option>Fake $11 deal</option></select><!-- Fake $12 deal --><p>Real $50 deal</p>'
        '<dialog open>Open $51 dialog</dialog>'
    )
    text = _assess("md", html).visible_text
    assert "Real $50 deal" in text and "Open $51 dialog" in text
    assert not any(f"Fake ${n} deal" in text for n in range(1, 13))


def test_closed_details_hides_body_but_keeps_summary() -> None:
    text = _assess("md", _page("<details><summary>Offers</summary><p>Hidden $99 offer</p>tail $98</details>")).visible_text
    assert "Offers" in text and "$99" not in text and "$98" not in text
    open_text = _assess("md", _page("<details open><summary>Offers</summary><p>Shown $99 offer</p></details>")).visible_text
    assert "$99" in open_text


def test_empty_thin_and_missing_html_fall_back() -> None:
    assert "empty_markdown" in _assess("", _page("<p>x</p>")).reasons
    missing = _assess("md", None)
    assert missing.needs_browser and missing.reasons == ("no_html",) and missing.visible_digest == ""
    thin = _assess("md", "<html><body><div id='root'></div></body></html>")
    assert thin.needs_browser and "thin_visible_text" in thin.reasons and thin.visible_digest == ""


def test_script_heavy_page_with_little_text_falls_back() -> None:
    scripts = "<script>1</script>" * 8
    assert "script_heavy_page" in _assess("md", _page(scripts)).reasons
    assert "script_heavy_page" not in _assess("md", _page("<script>1</script>" * 7)).reasons
    assert "script_heavy_page" not in _assess("md", _page(scripts + "<p>" + "word " * 400 + "</p>")).reasons


def test_claim_missing_from_visible_text_falls_back() -> None:
    result = _assess("md", _page('<p style="display:none">Botox $12 per unit</p>'), facts=["Botox $12 per unit"])
    assert result.needs_browser and "claim_not_in_visible_text" in result.reasons
    shown = _assess("md", _page("<p>Botox $12 per unit</p>"), facts=["no numbers here", "Botox $12 per unit"])
    assert not shown.needs_browser


def test_promo_image_requires_browser_only_when_visible() -> None:
    result = _assess("md", _page('<img src="/img/fall-special-banner.jpg" alt="Fall special 20% off">'))
    assert result.needs_browser and "image_promo_candidate" in result.reasons
    hidden = _assess("md", _page('<div style="display:none"><img src="/img/fall-special-banner.jpg"></div>'
                                 '<img src="/img/logo.png" alt="Glow">'))
    assert not hidden.needs_browser, hidden.reasons
