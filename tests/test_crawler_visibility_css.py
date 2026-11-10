from __future__ import annotations

FILLER = "Our medspa offers a full range of aesthetic services for every patient. " * 5


def _page(head: str, body: str) -> str:
    return f"<!doctype html><html><head><title>x</title>{head}</head><body><main>{FILLER}{body}</main></body></html>"


def _linked(css_site, css: str, body: str, facts=()):
    css_site.add("site.css", css)
    return css_site.assess(_page('<link rel="stylesheet" href="site.css">', body), facts)


def test_site_class_hidden_only_by_external_stylesheet(css_site) -> None:
    result = _linked(css_site, ".promo-card--expired{display:none}",
                     '<div class="promo-card--expired">Old Botox $9 unit</div><p>Botox $12 per unit</p>',
                     facts=["Botox $12 per unit"])
    assert "Old Botox $9" not in result.visible_text and "Botox $12 per unit" in result.visible_text
    assert not result.needs_browser, result.reasons


def test_later_rule_and_more_specific_rule_show_it_again(css_site) -> None:
    later = _linked(css_site, ".deal{display:none}\n.deal{display:block}", '<div class="deal">Later $11</div>')
    assert "Later $11" in later.visible_text
    specific = css_site.assess(_page(
        "<style>main .deal{display:block}</style><style>.deal{display:none}</style>"
        "<style>#d2{display:none}</style><style>.a.b.c{display:block}</style>",
        '<div class="deal">Specific $12</div><div id="d2" class="a b c">Id wins $13</div>'))
    assert "Specific $12" in specific.visible_text and "Id wins $13" not in specific.visible_text


def test_important_beats_more_specific_rule(css_site) -> None:
    result = _linked(css_site, ".deal{display:none !important}\n#d.deal{display:block}",
                     '<div id="d" class="deal">Important $14</div>')
    assert "Important $14" not in result.visible_text


def test_inline_style_beats_author_rules_but_not_author_important(css_site) -> None:
    result = _linked(css_site, ".deal{display:none}\n.pin{display:none !important}",
                     '<div class="deal" style="display:block">Inline $15</div>'
                     '<div class="pin" style="display:block">Pinned $16</div>')
    assert "Inline $15" in result.visible_text and "Pinned $16" not in result.visible_text


def test_media_queries_are_evaluated_at_desktop_width(css_site) -> None:
    css = ("@media (max-width: 768px){.mobile-off{display:none}}\n"
           "@media screen and (min-width: 1024px){.desktop-off{display:none}}\n"
           "@media print{.print-off{display:none}}\n"
           "@media not screen and (min-width: 2000px){.not-off{display:none}}\n"
           "@media (width >= 64rem){.range-off{display:none}}")
    result = _linked(css_site, css, '<p class="mobile-off">Mobile hidden $17</p><p class="desktop-off">Desktop $18</p>'
                     '<p class="print-off">Print $19</p><p class="not-off">Not $20</p><p class="range-off">Range $21</p>')
    assert "Mobile hidden $17" in result.visible_text and "Print $19" in result.visible_text
    assert "Desktop $18" not in result.visible_text and "Not $20" not in result.visible_text
    assert "Range $21" not in result.visible_text
    assert not result.needs_browser, result.reasons


def test_desktop_media_features_are_decided(css_site) -> None:
    css = ("@media (hover:hover){.h{display:none}}\n@media (hover:none){.hn{display:none}}\n"
           "@media (pointer:coarse){.pc{display:none}}\n@media (any-pointer:fine){.pf{display:none}}\n"
           "@media (prefers-reduced-motion:reduce){.rm{display:none}}\n@media (prefers-reduced-motion){.rb{display:none}}\n"
           "@media (forced-colors:active){.fc{display:none}}\n@media (hover){.hb{display:none}}\n"
           "@media (-webkit-min-device-pixel-ratio:2),(min-resolution:1.5dppx){.hi{display:none}}\n"
           "@media (max-resolution:96dpi){.lo{display:none}}\n@media (min-device-width:1200px){.dw{display:none}}\n"
           "@media (max-width:600px) or (hover:hover){.or{display:none}}\n@media (not (hover:none)){.nn{display:none}}\n"
           "@media ((min-width:1px) and (max-width:2px)){.nest{display:none}}\n"
           "@media screen and (min-width:1px) and screen and (max-width:2000px){.mal{display:none}}\n"
           "@media not screen and (min-width:1px) and print{.malnot{display:none}}\n"
           "@media (min-width:1px) and (hover:hover) or (pointer:fine){.mix{display:none}}\n"
           "@media only (min-width:1px){.only{display:none}}")
    hidden = ["h", "pf", "hb", "lo", "dw", "or", "nn"]
    shown = ["hn", "pc", "rm", "rb", "fc", "hi", "nest", "mal", "malnot", "mix", "only"]
    body = "".join(f'<p class="{name}">Class {name} $1</p>' for name in hidden + shown)
    result = _linked(css_site, css, body)
    assert not result.needs_browser, result.reasons
    assert all(f"Class {name} $1" not in result.visible_text for name in hidden)
    assert all(f"Class {name} $1" in result.visible_text for name in shown)


def test_rules_with_selectors_chrome_rejects_never_apply(css_site) -> None:
    css = (".a, .b:-ms-input-placeholder{display:none}\n.c::-moz-selection,.c{display:none}\n"
           ".d:not(.x,.y){display:none}\n.e:not(.x,.y){display:none}")
    result = _linked(css_site, css, '<p class="a">A $1</p><p class="c">C $2</p><p class="f">F $3</p>')
    assert "A $1" in result.visible_text and "C $2" in result.visible_text
    assert not result.needs_browser, result.reasons
    matched = css_site.assess(_page("<style>.f:nth-child(odd of .f){display:none}</style>", '<p class="f">F $3</p>'))
    assert matched.reasons == ("css_selector_undecidable",)


def test_link_media_attribute_and_async_media_swap(css_site) -> None:
    css_site.add("print.css", ".a{display:none}")
    css_site.add("async.css", ".b{display:none}")
    html = _page('<link rel="stylesheet" href="print.css" media="print">'
                 '<link rel="stylesheet" href="async.css" media="print" onload="this.media=\'all\'">',
                 '<p class="a">Shown $22</p><p class="b">Async hidden $23</p>')
    result = css_site.assess(html)
    assert "Shown $22" in result.visible_text and "Async hidden $23" not in result.visible_text


def test_imported_stylesheets_are_applied_before_the_importer(css_site) -> None:
    css_site.add("deep/base.css", ".x{display:none}\n.y{display:none}")
    css_site.add("narrow.css", ".z{display:none}")
    css = '@import url("deep/base.css");\n@import "narrow.css" (max-width: 600px);\n.y{display:block}'
    result = _linked(css_site, css, '<p class="x">Imported $24</p><p class="y">Reshown $25</p><p class="z">Narrow $26</p>')
    assert "Imported $24" not in result.visible_text
    assert "Reshown $25" in result.visible_text and "Narrow $26" in result.visible_text


def test_descendant_of_display_none_stays_hidden(css_site) -> None:
    result = _linked(css_site, ".panel{display:none}\n.panel p{display:block}",
                     '<div class="panel"><p>Nested $27</p></div>')
    assert "Nested $27" not in result.visible_text


def test_visibility_is_inherited_and_a_child_can_override_it(css_site) -> None:
    result = _linked(css_site, ".box{visibility:hidden}\n.box .shown{visibility:visible}",
                     '<div class="box">Parent $28<span class="shown">Child $29</span><span>Other $30</span></div>')
    assert "Child $29" in result.visible_text
    assert "Parent $28" not in result.visible_text and "Other $30" not in result.visible_text


def test_state_pseudo_classes_and_pseudo_elements_do_not_apply_at_rest(css_site) -> None:
    css = (".tip{display:none}\n.card:hover .tip{display:block}\n.card:focus-within .gone{display:none}\n"
           ".card::before{display:none}\n.card:not(:hover) .rest{display:none}")
    result = _linked(css_site, css, '<div class="card">Card $31<p class="tip">Hover $32</p><p class="gone">Focus $33</p>'
                     '<p class="rest">Rest $34</p></div>')
    assert "Card $31" in result.visible_text and "Focus $33" in result.visible_text
    assert "Hover $32" not in result.visible_text and "Rest $34" not in result.visible_text
    assert not result.needs_browser, result.reasons


def test_hiding_properties_from_stylesheets(css_site) -> None:
    css = (".sr-only{position:absolute;width:1px;height:1px;overflow:hidden;clip:rect(0,0,0,0)}\n"
           ".fade{opacity:0}\n.cv{content-visibility:hidden}\n.cp{clip-path:inset(50%)}\n"
           ".off{position:absolute;left:-9999px}\n.ind{text-indent:-9999px}\n"
           ".zero{font-size:0}\n.zero .reset{font-size:16px}\n.zero .rel{font-size:1.2em}\n"
           ".acc{max-height:0;overflow:hidden}\n.thin{width:0}\n.shift{left:-9999px}")
    body = ('<span class="sr-only">Sr $35</span><div class="fade"><p>Faded $36</p></div><div class="cv">Cv $37</div>'
            '<div class="cp">Clip $38</div><div class="off">Off $39</div><a class="ind">Indent $40</a>'
            '<div class="zero">Zero $41<span class="reset">Reset $42</span><span class="rel">Rel $43</span></div>'
            '<div class="acc">Accordion $44</div><div class="thin">Thin $45</div><div class="shift">Static $46</div>')
    result = _linked(css_site, css, body)
    for hidden in ("Sr $35", "Faded $36", "Cv $37", "Clip $38", "Off $39", "Indent $40", "Zero $41", "Rel $43",
                   "Accordion $44"):
        assert hidden not in result.visible_text, hidden
    for shown in ("Reset $42", "Thin $45", "Static $46"):
        assert shown in result.visible_text, shown


def test_values_chrome_rejects_are_dropped_and_near_zero_opacity_hides(css_site) -> None:
    css = (".ms{display:none}\n.ms{display:-ms-flexbox}\n.wk{display:none}\n.wk{display:-webkit-box}\n"
           ".two{display:none}\n.two{display:inline flex}\n.vis{visibility:hidden}\n.vis{visibility:maybe}\n"
           ".pos{position:absolute;left:-9999px}\n.pos{position:-ms-page}\n.ov{width:0}\n.ov{overflow:hidden}\n"
           ".ov{overflow:wobbly}\n.cv{content-visibility:hidden}\n.cv{content-visibility:nope}\n.faint{opacity:0.0001}")
    body = ('<p class="ms">Ms $1</p><p class="wk">Webkit $2</p><p class="two">Two $3</p><p class="vis">Vis $4</p>'
            '<p class="pos">Pos $5</p><p class="ov">Overflow $6</p><p class="cv">Cv $7</p><p class="faint">Faint $8</p>')
    result = _linked(css_site, css, body)
    for hidden in ("Ms $1", "Vis $4", "Pos $5", "Overflow $6", "Cv $7", "Faint $8"):
        assert hidden not in result.visible_text, hidden
    assert "Webkit $2" in result.visible_text and "Two $3" in result.visible_text


def test_author_display_overrides_the_hidden_attribute_and_guess_classes_are_gone(css_site) -> None:
    result = _linked(css_site, ".flex{display:flex}",
                     '<div hidden class="flex">Flex $47</div><div hidden>Attr $48</div>'
                     '<div class="d-none sr-only">Unstyled $49</div><p aria-hidden="true">Aria $50</p>')
    assert "Flex $47" in result.visible_text and "Attr $48" not in result.visible_text
    assert "Unstyled $49" in result.visible_text and "Aria $50" in result.visible_text
    assert not result.needs_browser, result.reasons


def test_custom_properties_and_cascade_layers(css_site) -> None:
    css = (".e-con{display:var(--display, block)}\n.off{--display:none}\n.missing{display:var(--nope)}\n"
           "@layer base, utilities;\n@layer utilities{#u{display:block}}\n.lay{display:none}\n"
           "@layer base{.imp{display:none !important}}\n.imp{display:block !important}")
    body = ('<div class="e-con">Default $51</div><div class="off"><div class="e-con">Var $52</div></div>'
            '<div class="missing">Missing var $53</div><div id="u" class="lay">Layered $54</div>'
            '<div class="imp">Layer important $55</div>')
    result = _linked(css_site, css, body)
    assert "Default $51" in result.visible_text and "Missing var $53" in result.visible_text
    assert "Var $52" not in result.visible_text and "Layered $54" not in result.visible_text
    assert "Layer important $55" not in result.visible_text
