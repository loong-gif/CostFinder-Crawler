from __future__ import annotations

import time

from utils.crawler_visibility_fetch import fetch_stylesheets

FILLER = "Our medspa offers a full range of aesthetic services for every patient. " * 5


def _page(head: str, body: str, filler: str = FILLER) -> str:
    return f"<!doctype html><html><head><title>x</title>{head}</head><body><main>{filler}{body}</main></body></html>"


def _styled(css_site, css: str, body: str):
    return css_site.assess(_page(f"<style>{css}</style>", body))


def _check(result, hidden=(), shown=()) -> None:
    assert not result.needs_browser, result.reasons
    for text in hidden:
        assert text not in result.visible_text, text
    for text in shown:
        assert text in result.visible_text, text


def test_later_declaration_of_the_same_property_wins_within_a_rule_and_inline_style(css_site) -> None:
    result = _styled(css_site, ".dup{display:none;display:block}\n.dup2{display:block;display:none}",
                     '<p class="dup">Dup $1</p><p class="dup2">Dup2 $2</p>'
                     '<p style="display:none;display:block">Inline $3</p><p style="display:block;display:none">Inline2 $4</p>')
    _check(result, hidden=["Dup2 $2", "Inline2 $4"], shown=["Dup $1", "Inline $3"])


def test_long_custom_property_chains_resolve_and_only_cycles_are_invalid(css_site) -> None:
    chain = "".join(f"--v{n}:var(--v{n + 1});" for n in range(24)) + "--v24:none;"
    result = _styled(css_site, f":root{{{chain}}}\n.deep{{display:var(--v0)}}\n"
                               ".self{--s:var(--s, none);display:var(--s, none)}\n.fb{display:var( --nope , none)}\n"
                               ".self2{--t:var(--t, none);display:var(--t)}",
                     '<p class="deep">Deep $1</p><p class="self">Self $2</p><p class="fb">Fallback $3</p>'
                     '<p class="self2">Self2 $4</p>')
    _check(result, hidden=["Deep $1", "Self $2", "Fallback $3"], shown=["Self2 $4"])


def test_thresholds_of_the_hiding_properties(css_site) -> None:
    css = (".l100{position:relative;left:-100px}\n.l99{position:relative;left:-99px}\n"
           ".ti100{text-indent:-100px}\n.ti99{text-indent:-99px}\n.ox{overflow-x:hidden;width:0}\n"
           ".oy{height:0;overflow:visible hidden}\n.w1{width:1px;overflow:hidden}\n.w2{width:2px;overflow:hidden}\n"
           ".op01{opacity:0.01}\n.op-pct{opacity:0.5%}\n.c-comma{position:absolute;clip:rect(0,0,0,0)}\n"
           ".c-space{position:fixed;clip:rect(0px 0px 0px 0px)}\n.c-w{position:absolute;clip:rect(10px,11px,200px,10px)}\n"
           ".c-h{position:absolute;clip:rect(10px,200px,11px,10px)}\n.c-three{position:absolute;clip:rect(0,0,0)}\n"
           ".c-fn{position:absolute;clip:inset(0)}\n.cp50{clip-path:inset(50%)}\n.cp49{clip-path:inset(49%)}\n"
           ".blk{width:calc((100% - 10px))}\n.k3{display:none}\n.k3{display:block flow list-item}\n"
           ".v2{visibility:hidden}\n.v2{visibility:visible hidden}\n.v3{visibility:hidden}\n.v3{visibility:block flow}\n"
           ".nth:nth-child(1){display:none}")
    names = ["l100", "l99", "ti100", "ti99", "ox", "oy", "w1", "w2", "op01", "op-pct", "c-comma", "c-space", "c-w",
             "c-h", "c-three", "c-fn", "cp50", "cp49", "blk", "k3", "v2", "v3"]
    body = "".join(f'<p class="{name}">Box {name} $1</p>' for name in names) + '<div><p class="nth">Nth $2</p></div>'
    result = _styled(css_site, css, body)
    hidden = ["l100", "ti100", "ox", "oy", "w1", "op-pct", "c-comma", "c-space", "c-w", "c-h", "cp50", "v2", "v3"]
    _check(result, hidden=[f"Box {n} $1" for n in hidden] + ["Nth $2"],
           shown=[f"Box {n} $1" for n in names if n not in hidden])


def test_closed_details_shows_only_its_first_direct_summary(css_site) -> None:
    result = css_site.assess(_page("", "<details><div><summary>Inner $1</summary></div><summary>Real $2</summary>"
                                       "<summary>Second $3</summary></details>"))
    _check(result, hidden=["Inner $1", "Second $3"], shown=["Real $2"])


def test_more_media_and_supports_edges(css_site) -> None:
    css = ("@media (max-width:600px) or (pointer:coarse){.a{display:none}}\n@media only screen{.b{display:none}}\n"
           "@media only{.c{display:none}}\n@media not (not and){.d{display:none}}\n@media (hover:hover) and{.e{display:none}}\n"
           "@media screen foo (hover:hover){.f{display:none}}\n@supports (--x:){.g{display:none}}\n"
           "@supports (--a:1) (--b:2){.h{display:none}}\n@media (>= 5px){.nomatch{display:none}}")
    names = "abcdefgh"
    result = _styled(css_site, css, "".join(f'<p class="{n}">Q{n} $1</p>' for n in names))
    _check(result, hidden=["Qb $1", "Qg $1"], shown=[f"Q{n} $1" for n in "acdefh"])
    undecided = _styled(css_site, "@media not (orientation:portrait){.a{display:none}}\n"
                                  "@media (>= 5px){.b{display:none}}", '<p class="a">A $1</p>')
    assert undecided.reasons == ("css_condition_undecidable",)


def test_imports_with_undecidable_media_and_leading_comments(css_site) -> None:
    css_site.add("portrait.css", ".p{display:none}")
    css_site.add("one.css", ".one{display:none}")
    css_site.add("site.css", '/* banner */\n@import "one.css";\n@import "portrait.css" (orientation:portrait);')
    result = css_site.assess(_page('<link rel="stylesheet" href="site.css">', '<p class="one">One $1</p><p class="p">P $2</p>'))
    assert result.reasons == ("css_condition_undecidable",) and "One $1" not in result.visible_text


def test_layer_statement_orders_layers_before_their_blocks(css_site) -> None:
    css = "@layer p1, q1;\n@layer r1{.t{display:none}}\n@layer q1{.t{display:block}}"
    _check(_styled(css_site, css, '<p class="t">T $1</p>'), hidden=["T $1"])


def test_preload_links_are_not_stylesheets_until_their_onload_swaps_them(css_site) -> None:
    html = _page('<link rel="preload" as="font" href="font.woff2" onload="this.rel=\'stylesheet\'">'
                 '<link rel="preload" as="style" href="pre.css"><link rel="icon" href="icon.css" onload="stylesheet">',
                 "<p>Plain $1</p>")
    assert fetch_stylesheets(css_site.url("page.html"), html) == {}


def test_text_length_thresholds(css_site) -> None:
    exact = "x" * 200
    assert "thin_visible_text" not in css_site.assess(_page("", f"<p>{exact}</p>", filler="")).reasons
    assert "thin_visible_text" in css_site.assess(_page("", f"<p>{exact[:-1]}</p>", filler="")).reasons
    scripts = "<script>1</script>" * 8
    long_text = "y" * 2000
    assert "script_heavy_page" not in css_site.assess(_page("", scripts + f"<p>{long_text}</p>", filler="")).reasons
    assert "script_heavy_page" in css_site.assess(_page("", scripts + f"<p>{long_text[:-1]}</p>", filler="")).reasons


def test_retries_wait_longer_each_time_and_a_400_is_final(css_site) -> None:
    css_site.add_responses("busy.css", [(503, "busy"), (503, "busy"), (200, ".a{display:none}")])
    css_site.add_responses("bad.css", [(400, "bad"), (200, ".b{display:none}")])
    html = _page('<link rel="stylesheet" href="busy.css"><link rel="stylesheet" href="bad.css">', "")
    started = time.monotonic()
    sheets = fetch_stylesheets(css_site.url("page.html"), html)
    assert time.monotonic() - started >= 0.85
    assert sheets[css_site.url("busy.css")] == ".a{display:none}" and sheets[css_site.url("bad.css")] is None
