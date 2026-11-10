from __future__ import annotations

FILLER = "Our medspa offers a full range of aesthetic services for every patient. " * 5


def _page(head: str, body: str) -> str:
    return f"<!doctype html><html><head><title>x</title>{head}</head><body><main>{FILLER}{body}</main></body></html>"


def _styled(css_site, css: str, body: str):
    return css_site.assess(_page(f"<style>{css}</style>", body))


def _check(result, hidden=(), shown=()) -> None:
    for text in hidden:
        assert text not in result.visible_text, text
    for text in shown:
        assert text in result.visible_text, text


def test_media_queries_that_cannot_be_decided_or_are_malformed(css_site) -> None:
    undecided = ["@media (max-width:600px) or (orientation:portrait){.a{display:none}}",
                 "@media (min-width:5){.a{display:none}}", "@media (min-resolution:2){.a{display:none}}",
                 "@media (width >= 50vw){.a{display:none}}", "@media (400px < 500px){.a{display:none}}",
                 "@media (width 5px 6px){.a{display:none}}", "@media (min-width:1px) and foo(x){.a{display:none}}",
                 "@media (hover:hover) and (weird){.a{display:none}}"]
    for css in undecided:
        assert _styled(css_site, css, '<p class="a">A $1</p>').reasons == ("css_condition_undecidable",), css
    malformed = ["@media (hover) (pointer){.a{display:none}}", "@media not (hover) and{.a{display:none}}",
                 "@media screen or (hover){.a{display:none}}", "@media not (hover) (x){.a{display:none}}"]
    for css in malformed:
        result = _styled(css_site, css, '<p class="a">A $1</p>')
        assert not result.needs_browser and "A $1" in result.visible_text, css


def test_media_ranges_and_resolutions_at_the_desktop_screen(css_site) -> None:
    css = ("@media (1000px < width <= 1440px){.r1{display:none}}\n@media (1440px < width){.r2{display:none}}\n"
           "@media (width > 1000px){.r3{display:none}}\n@media (width < 1440px){.r4{display:none}}\n"
           "@media (width = 1440px){.r5{display:none}}\n@media (max-width:89.9em){.r6{display:none}}\n"
           "@media (min-resolution:96dpi){.r7{display:none}}\n@media (min-resolution:1dpcm){.r8{display:none}}\n"
           "@media (-webkit-max-device-pixel-ratio:1){.r9{display:none}}\n@media (min-width:0){.r10{display:none}}\n"
           "@media (min-width:1441px){.r11{display:none}}\n@media (max-width:1439.5px){.r12{display:none}}")
    body = "".join(f'<p class="r{n}">R{n} $1</p>' for n in range(1, 13))
    result = _styled(css_site, css, body)
    assert not result.needs_browser, result.reasons
    _check(result, hidden=["R1 $1", "R3 $1", "R5 $1", "R7 $1", "R8 $1", "R9 $1", "R10 $1"],
           shown=["R2 $1", "R4 $1", "R6 $1", "R11 $1", "R12 $1"])


def test_nested_rules_container_queries_and_cycles(css_site) -> None:
    nested = _styled(css_site, ".card{color:red; .deal{display:none}}", '<div class="card">Card $1</div>')
    assert nested.reasons == ("css_selector_undecidable",)
    quiet = _styled(css_site, ".card{color:red; .deal{color:blue} @media print{.x{color:red}}}\n"
                              ".gone{ .deal{display:none} }", '<div class="card">Card $1</div>')
    assert not quiet.needs_browser, quiet.reasons
    container = _styled(css_site, "@container (min-width:1px){.card{display:none}}", '<div class="card">Card $1</div>')
    assert container.reasons == ("css_condition_undecidable",) and "Card $1" in container.visible_text
    css_site.add("a.css", '@import "b.css";\n.a{display:none}')
    css_site.add("b.css", '@import "a.css";\n.b{display:none}')
    cycle = css_site.assess(_page('<link rel="stylesheet" href="a.css">', '<p class="a">A $1</p><p class="b">B $2</p>'))
    _check(cycle, hidden=["A $1", "B $2"])
    assert not cycle.needs_browser, cycle.reasons


def test_import_preamble_rules(css_site) -> None:
    css_site.add("one.css", ".one{display:none}")
    css_site.add("two.css", ".two{display:none}")
    css_site.add("late.css", ".late{display:none}")
    css_site.add("site.css", '@charset "utf-8";\n@layer base;\n@import url(one.css);\n@import bad;\n@import url();\n'
                             '@import url("two.css") print;\n.x{color:red}\n@import "late.css";')
    result = css_site.assess(_page('<link rel="stylesheet" href="site.css">',
                                   '<p class="one">One $1</p><p class="two">Two $2</p><p class="late">Late $3</p>'))
    _check(result, hidden=["One $1"], shown=["Two $2", "Late $3"])
    assert not result.needs_browser, result.reasons


def test_layers_order_by_first_appearance_and_nesting(css_site) -> None:
    css = ("@layer {.anon{display:none}}\n.anon{display:block}\n"
           "@layer outer{.p{display:none} @layer inner{.p{display:block}}}\n"
           "@layer b{.q{display:none}}\n@layer a{.q{display:block}}\n@layer a, b;\n"
           "@layer a2, b2;\n@layer b2{.q2{display:none}}\n@layer a2{.q2{display:block}}\n"
           "@layer x.y{.s{display:block}}\n@layer x{.s{display:none}}")
    result = _styled(css_site, css, '<p class="anon">Anon $1</p><p class="p">P $2</p><p class="q">Q $3</p>'
                                    '<p class="q2">Q2 $5</p><p class="s">S $4</p>')
    _check(result, hidden=["P $2", "Q2 $5", "S $4"], shown=["Anon $1", "Q $3"])


def test_value_edge_cases(css_site) -> None:
    css = (".cyc{--a:var(--b);--b:var(--a);display:var(--a)}\n.badvar{--x:var(--missing);display:var(--x, none)}\n"
           ".rect-auto{position:absolute;clip:rect(auto,auto,auto,auto)}\n"
           ".rect-big{position:absolute;clip:rect(0,100px,100px,0)}\n.rect-static{clip:rect(0,0,0,0)}\n"
           ".inset-px{clip-path:inset(10px)}\n.inset-low{clip-path:inset(20%)}\n.pct0{opacity:0%}\n.pct50{opacity:50%}\n"
           ".parent{display:none}\n.inh{display:inherit}\n.rev{display:revert}\n.ini{visibility:hidden}\n"
           ".ini span{visibility:initial}\n.uns{visibility:hidden}\n.uns span{visibility:unset}\n"
           ".num{display:5}\n.kw{display:block flex grid ruby}\n.opx{opacity:none}\n.two-op{opacity:0 1}\n"
           ".ti{text-indent:-10em}\n.ti2{text-indent:5%}\n.fpx{font-size:12px}\n"
           ".clip-auto{position:absolute;clip:auto}\n.cp-none{clip-path:none}\n.perr{ 5px; display:none }\n"
           ".unused{--zzz:1}")
    body = ('<p class="cyc">Cycle $1</p><p class="badvar">Badvar $2</p><p class="rect-auto">RectAuto $3</p>'
            '<p class="rect-big">RectBig $4</p><p class="rect-static">RectStatic $5</p><p class="inset-px">InsetPx $6</p>'
            '<p class="inset-low">InsetLow $7</p><p class="pct0">Pct0 $8</p><p class="pct50">Pct50 $9</p>'
            '<div class="ini">Ini $10<span>IniChild $11</span></div><div class="uns">Uns $12<span>UnsChild $13</span></div>'
            '<p class="num">Num $14</p><p class="kw">Kw $15</p><p class="opx">Opx $16</p><p class="two-op">TwoOp $17</p>'
            '<p class="ti">Ti $18</p><p class="ti2">Ti2 $19</p><p class="fpx">Fpx $20</p>'
            '<p class="clip-auto">ClipAuto $23</p><p class="cp-none">CpNone $24</p><p class="perr">Perr $25</p>')
    result = _styled(css_site, css, body)
    assert not result.needs_browser, result.reasons
    _check(result, shown=["ClipAuto $23", "CpNone $24"], hidden=["Perr $25"])
    _check(result, hidden=["Badvar $2", "Pct0 $8", "Ini $10", "Uns $12", "UnsChild $13", "Ti $18"],
           shown=["Cycle $1", "RectAuto $3", "RectBig $4", "RectStatic $5", "InsetPx $6", "InsetLow $7", "Pct50 $9",
                  "IniChild $11", "Num $14", "Kw $15", "Opx $16", "TwoOp $17", "Ti2 $19", "Fpx $20"])
    inherit = _styled(css_site, ".parent{display:none}\n.inh{display:inherit}\n.wrap{display:block}",
                      '<div class="wrap"><p class="inh">Inh $21</p></div>')
    _check(inherit, shown=["Inh $21"])
    revert = _styled(css_site, ".rev{display:revert}", '<p class="rev">Rev $22</p>')
    assert revert.reasons == ("css_value_undecidable",)


def test_stylesheet_timeout_is_unavailable(css_site) -> None:
    css_site.add("slow.css", ".a{display:none}", delay=1.5)
    result = css_site.assess(_page('<link rel="stylesheet" href="slow.css">', '<p class="a">A $1</p>'), timeout=0.3)
    assert result.reasons == ("stylesheet_unavailable",)
