from __future__ import annotations

import socket

from utils.crawler_visibility import assess_page
from utils.crawler_visibility_fetch import fetch_stylesheets

FILLER = "Our medspa offers a full range of aesthetic services for every patient. " * 5


def _page(head: str, body: str) -> str:
    return f"<!doctype html><html><head><title>x</title>{head}</head><body><main>{FILLER}{body}</main></body></html>"


def _closed_port_url() -> str:
    with socket.socket() as sock:
        sock.bind(("0.0.0.0", 0))
        port = sock.getsockname()[1]
    return f"http://{socket.gethostname()}:{port}/gone.css"


def test_unfetchable_stylesheet_makes_the_page_uncertain(css_site) -> None:
    css_site.add("locked.css", ".a{display:none}", status=403)
    css_site.add("auth.css", ".a{display:none}", status=401)
    html = _page(f'<link rel="stylesheet" href="locked.css"><link rel="stylesheet" href="auth.css">'
                 f'<link rel="stylesheet" href="{_closed_port_url()}">', "<p>Botox $12 per unit</p>")
    result = css_site.assess(html)
    assert result.needs_browser and result.reasons == ("stylesheet_unavailable",)
    sheets = fetch_stylesheets(css_site.url("page.html"), html)
    assert set(sheets.values()) == {None} and len(sheets) == 3


def test_missing_stylesheet_is_an_empty_sheet_as_in_chrome(css_site) -> None:
    css_site.add("gone.css", ".a{display:none}", status=410)
    html = _page('<link rel="stylesheet" href="never-existed.css"><link rel="stylesheet" href="gone.css">',
                 '<p class="a">Shown $1</p>')
    sheets = fetch_stylesheets(css_site.url("page.html"), html)
    assert sheets == {css_site.url("never-existed.css"): "", css_site.url("gone.css"): ""}
    result = css_site.assess(html)
    assert not result.needs_browser and "Shown $1" in result.visible_text, result.reasons
    assert css_site.hit_count("gone.css") == 1


def test_stylesheet_urls_rejected_by_the_url_guard_are_not_fetched(css_site) -> None:
    css_site.add("private.css", ".a{display:none}")
    css_site.add("imported.css", ".b{display:none}")
    css_site.add("site.css", f'@import url("{css_site.loopback_url("imported.css")}");')
    html = _page(f'<link rel="stylesheet" href="{css_site.loopback_url("private.css")}">'
                 '<link rel="stylesheet" href="http://10.0.0.5/x.css"><link rel="stylesheet" href="data:text/css,p{}">'
                 '<link rel="stylesheet" href="site.css">', '<p class="a">A $1</p>')
    sheets = fetch_stylesheets(css_site.url("page.html"), html)
    assert sheets[css_site.loopback_url("private.css")] is None and sheets["http://10.0.0.5/x.css"] is None
    assert sheets["data:text/css,p{}"] is None and sheets[css_site.loopback_url("imported.css")] is None
    assert css_site.hit_count("private.css") == 0 and css_site.hit_count("imported.css") == 0
    assert css_site.assess(html).reasons == ("stylesheet_unavailable",)


def test_redirects_are_followed_only_to_addresses_the_guard_accepts(css_site) -> None:
    css_site.add("target.css", ".a{display:none}")
    css_site.add("private.css", ".b{display:none}")
    css_site.add("moved.css", "target.css", status=301)
    css_site.add("sneaky.css", css_site.loopback_url("private.css"), status=302)
    html = _page('<link rel="stylesheet" href="moved.css"><link rel="stylesheet" href="sneaky.css">', "")
    sheets = fetch_stylesheets(css_site.url("page.html"), html)
    assert sheets == {css_site.url("moved.css"): ".a{display:none}", css_site.url("sneaky.css"): None}
    assert css_site.hit_count("target.css") == 1 and css_site.hit_count("private.css") == 0
    for hop in range(6):
        css_site.add(f"hop{hop}.css", f"hop{hop + 1}.css", status=307)
    css_site.add("hop6.css", ".c{display:none}")
    css_site.add("choices.css", ".d{display:none}", status=300)
    chained = fetch_stylesheets(css_site.url("page.html"), _page(
        '<link rel="stylesheet" href="hop1.css"><link rel="stylesheet" href="hop0.css">'
        '<link rel="stylesheet" href="choices.css">', ""))
    assert chained == {css_site.url("hop1.css"): ".c{display:none}", css_site.url("hop0.css"): None,
                       css_site.url("choices.css"): None}
    css_site.add("loop.css", "loop.css", status=302)
    assert fetch_stylesheets(css_site.url("page.html"), _page('<link rel="stylesheet" href="loop.css">', "")) == {
        css_site.url("loop.css"): None}


def test_transient_stylesheet_failures_are_retried_before_the_page_is_judged(css_site) -> None:
    css_site.add_responses("busy.css", [(503, "busy"), (429, "slow down"), (200, ".a{display:none}")])
    css_site.add_responses("down.css", [(503, "busy")])
    css_site.add_responses("gone.css", [(404, "missing"), (200, ".b{display:none}")])
    html = _page('<link rel="stylesheet" href="busy.css"><link rel="stylesheet" href="down.css">'
                 '<link rel="stylesheet" href="gone.css">', '<p class="a">Busy $1</p>')
    sheets = fetch_stylesheets(css_site.url("page.html"), html)
    assert sheets[css_site.url("busy.css")] == ".a{display:none}"
    assert sheets[css_site.url("down.css")] is None and sheets[css_site.url("gone.css")] == ""
    assert css_site.hit_count("busy.css") == 3 and css_site.hit_count("down.css") == 3
    assert css_site.hit_count("gone.css") == 1


def test_uncompilable_selector_with_hiding_declarations_makes_the_page_uncertain(css_site) -> None:
    css_site.add("site.css", ".deal:unknown-state{display:none}\n.deal:-moz-thing{color:red}\n"
                             r".absent [\:has\(.x\)] .deal{display:none}")
    result = css_site.assess(_page('<link rel="stylesheet" href="site.css">', '<p class="deal">Deal $12</p>'))
    assert result.needs_browser and result.reasons == ("css_selector_undecidable",)
    css_site.add("quiet.css", r".absent [\:has\(.x\)] .deal, #nothing:unknown-state{display:none}")
    quiet = css_site.assess(_page('<link rel="stylesheet" href="quiet.css">', '<p class="deal">Deal $12</p>'))
    assert not quiet.needs_browser, quiet.reasons


def test_chrome_state_pseudo_classes_unknown_to_soupsieve_never_match_at_rest(css_site) -> None:
    css_site.add("site.css", ".a:-webkit-autofill{display:none}\n.b:not(:-webkit-autofill){display:none}\n"
                             "dialog:modal{display:block}\n.c:-webkit-any-link{display:none}\n"
                             ".d:not(.x,#y){display:none}\n#e.d{display:block}\n.f:not(#x,.y){display:none}\n.f.k.l.m{display:block}\n"
                             "@supports (--test: true){.h{display:none}}\n@supports not (--test: true){.i{display:none}}")
    html = _page('<link rel="stylesheet" href="site.css">',
                 '<p class="a">A $1</p><p class="b">B $2</p><dialog>Closed $3</dialog><a class="c" href="/x">C $4</a>'
                 '<p id="e" class="d">D $5</p><p class="f k l m">F $6</p><p class="h">H $7</p><p class="i">I $8</p>')
    result = css_site.assess(html)
    assert not result.needs_browser, result.reasons
    assert "A $1" in result.visible_text and "B $2" not in result.visible_text and "Closed $3" not in result.visible_text
    assert "C $4" not in result.visible_text and "D $5" in result.visible_text and "F $6" not in result.visible_text
    assert "H $7" not in result.visible_text and "I $8" in result.visible_text


def test_undecidable_media_query_matters_only_when_its_rule_can_hide_a_matched_element(css_site) -> None:
    css_site.add("site.css", "@media (orientation: portrait){.deal{display:none}.nomatch{display:none}}\n"
                             "@media (min-resolution: 2dppx){.deal{color:red}}\n"
                             "@media (min-width: 40vw){.nomatch{display:none}}")
    html = _page('<link rel="stylesheet" href="site.css">', '<p class="deal">Deal $12</p>')
    result = css_site.assess(html)
    assert result.needs_browser and result.reasons == ("css_condition_undecidable",)
    css_site.add("quiet.css", "@media (orientation: portrait){.nomatch{display:none}}\n"
                              "@supports (display:grid){.nomatch{display:none}}")
    quiet = css_site.assess(_page('<link rel="stylesheet" href="quiet.css">', '<p class="deal">Deal $12</p>'))
    assert not quiet.needs_browser, quiet.reasons


def test_rules_inside_supports_and_container_blocks_are_undecidable_when_they_match(css_site) -> None:
    css_site.add("site.css", "@supports (display:grid){.deal{display:none}}\n@starting-style{.deal{opacity:0}}")
    result = css_site.assess(_page('<link rel="stylesheet" href="site.css">', '<p class="deal">Deal $12</p>'))
    assert result.reasons == ("css_condition_undecidable",) and "Deal $12" in result.visible_text


def test_stylesheets_are_fetched_once_per_url_and_imports_are_followed(css_site) -> None:
    css_site.add("base.css", ".x{display:none}")
    css_site.add("site.css", '@import url("base.css");\n@import url("base.css");')
    html = _page('<link rel="stylesheet" href="site.css"><link rel="stylesheet" href="site.css">', "<p>x</p>")
    page_url = css_site.url("page.html")
    first = fetch_stylesheets(page_url, html)
    second = fetch_stylesheets(page_url, html)
    assert first == second and set(first) == {css_site.url("site.css"), css_site.url("base.css")}
    assert css_site.hit_count("site.css") == 1 and css_site.hit_count("base.css") == 1


def test_stylesheets_skip_noscript_alternate_and_disabled_links_and_honour_base_href(css_site) -> None:
    css_site.add("sub/real.css", ".a{display:none}")
    html = _page('<base href="sub/"><noscript><link rel="stylesheet" href="ns.css"></noscript>'
                 '<link rel="alternate stylesheet" href="alt.css"><link rel="stylesheet" href="off.css" disabled>'
                 '<link rel="preload" as="style" href="real.css" onload="this.rel=\'stylesheet\'">',
                 '<p class="a">Hidden $1</p>')
    sheets = fetch_stylesheets(css_site.url("page.html"), html)
    assert list(sheets) == [css_site.url("sub/real.css")]
    result = assess_page("md", html, (), page_url=css_site.url("page.html"), stylesheets=sheets)
    assert "Hidden $1" not in result.visible_text and not result.needs_browser, result.reasons


def test_stylesheet_missing_from_the_supplied_texts_is_unavailable(css_site) -> None:
    html = _page('<link rel="stylesheet" href="site.css">', "<p>Botox $12 per unit</p>")
    result = assess_page("md", html, (), page_url=css_site.url("page.html"), stylesheets={})
    assert result.reasons == ("stylesheet_unavailable",)


def test_carousel_markup_and_embedded_frames_still_need_the_browser(css_site) -> None:
    result = css_site.assess(_page("", '<div class="swiper"><div class="swiper-slide">Slide $5</div></div>'))
    assert result.needs_browser and "js_widget_state" in result.reasons
    framed = css_site.assess(_page("", '<iframe src="https://book.example/specials"></iframe>'))
    assert framed.needs_browser and "embedded_content" in framed.reasons
    for markup in ('<div data-aos="fade-up">Deal $5</div>', '<div data-animation="fadeIn">Deal $5</div>',
                   '<div class="wow fadeInUp">Deal $5</div>'):
        animated = css_site.assess(_page("", markup))
        assert animated.reasons == ("js_animation",), markup
    hidden = css_site.assess(_page("", '<div hidden><div class="swiper" data-aos="x"><iframe></iframe></div></div>'
                                       '<section data-animation="none">Static $6</section>'))
    assert not hidden.needs_browser, hidden.reasons
