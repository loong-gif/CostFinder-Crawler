from __future__ import annotations

from utils.linked_terms import attach_terms, collect_linked_terms, terms_links, terms_sections
from utils.visibility_manifest import build_manifest, build_shard

PROMOTIONS_MD = """# Promotions

### Your First Botox Treatment Is $10 Per Unit‡‡

Our #1 wrinkle relaxer for a smooth, youthful look.

Subject to [offer terms](/offer-terms/#btx)

### 40% Off Your First IPL Photofacial‡

Subject to [offer terms](/offer-terms/#ipl)

[Book Now](/book-now/#top) [Gift Cards](/gift-card/) [Partner terms](https://other.example/terms/#x)
"""

TERMS_HTML = """<html><body>
<nav><a id="btx" href="/book-now/">Claim Offer</a></nav>
<h1>Offer Terms &amp; Conditions</h1>
<h2>LASERAWAY NEW PATIENT &amp; VALUE SAVINGS</h2>
<div><h2 id="ipl">IPL New Patient Promotion</h2>
<p>‡Valid on purchases made by phone or in person. Available only to new patients. Valid for the Face area only.</p></div>
<div><h2 id="btx">Botox New Patient Promotion</h2>
<p>‡‡Valid on purchases made by phone or in person. Receive your first Botox purchase of 20 to 300 units for
$10 per unit (residents of NV can only purchase in batches of 50).</p>
<script>var tracking = 1;</script></div>
<div><h2 id="xeo">Xeomin New Patient Promotion</h2><p>^^Receive your first Xeomin purchase for $7 per unit.</p></div>
</body></html>"""

PAGE = "https://www.laseraway.com/promotions/"
TERMS = "https://www.laseraway.com/offer-terms/"


def test_terms_links_keep_same_site_terms_anchors_with_their_card_heading() -> None:
    assert terms_links(PROMOTIONS_MD, PAGE) == [(TERMS, "btx", "Your First Botox Treatment Is $10 Per Unit‡‡"),
                                                (TERMS, "ipl", "40% Off Your First IPL Photofacial‡")]


def test_terms_section_prefers_the_heading_and_stops_at_the_next_heading() -> None:
    sections = terms_sections(TERMS_HTML, ["btx", "ipl", "missing"])
    # 导航按钮带有同名 id，取标题元素；段落止于下一个同级标题，脚本内容不计入
    assert sections["btx"] == ("Botox New Patient Promotion ‡‡Valid on purchases made by phone or in person. Receive "
                               "your first Botox purchase of 20 to 300 units for $10 per unit (residents of NV can "
                               "only purchase in batches of 50).")
    assert sections["ipl"].startswith("IPL New Patient Promotion ‡Valid on purchases")
    assert "Botox" not in sections["ipl"]
    assert "missing" not in sections


def test_each_terms_page_is_fetched_once_and_its_sections_are_attached_to_the_linking_cards() -> None:
    fetched: list[str] = []

    def fetch(url: str) -> str:
        fetched.append(url)
        return TERMS_HTML

    linked = collect_linked_terms(PROMOTIONS_MD, PAGE, fetch)
    assert fetched == [TERMS]
    assert [(t["url"], t["cards"]) for t in linked] == [
        (f"{TERMS}#btx", ["Your First Botox Treatment Is $10 Per Unit‡‡"]),
        (f"{TERMS}#ipl", ["40% Off Your First IPL Photofacial‡"])]
    # LLM 卡片标题去掉了脚注符号，仍对应到同一张卡片；没有条款链接的卡片保持不变
    blocks = attach_terms([["Your First Botox Treatment Is $10 Per Unit", "Our #1 wrinkle relaxer."],
                           ["Hydrafacial $99", "This month only"]], linked)
    assert blocks[0][:2] == ["Your First Botox Treatment Is $10 Per Unit", "Our #1 wrinkle relaxer."]
    assert blocks[0][2].startswith("Botox New Patient Promotion ‡‡Valid on purchases") and len(blocks[0]) == 3
    assert blocks[1] == ["Hydrafacial $99", "This month only"]


CHROME = ["Home", "About Us", "Services", "Book Now", "Contact",
          "Our board-certified injectors have served the community for over ten years with natural results.",
          "Call us today to schedule a complimentary consultation with one of our aesthetic specialists.",
          "Hours: Monday to Friday 9am - 6pm, Saturday 10am - 4pm. Located downtown near the main square."]


def _plan(card: str) -> dict:
    return {"entries": [{"kind": "promotion", "action": "insert", "source_url": PAGE, "source_pages": [PAGE],
                         "linked_terms_pages": [TERMS], "content": [card]}]}


def _render(terms_texts: list[str]) -> dict:
    return {"results": [
        {"url": PAGE, "checked_at": "t", "result": {"texts": [*CHROME, "Your First Botox Treatment Is $10 Per Unit‡‡"]}},
        {"url": TERMS, "checked_at": "t", "result": {"texts": [*CHROME, *terms_texts]}}]}


def test_card_lines_from_the_linked_terms_are_verified_on_the_rendered_terms_page() -> None:
    card = "Your First Botox Treatment Is $10 Per Unit\nFirst Botox purchase of 20 to 300 units for $10 per unit"
    shard = build_shard(_plan(card), "run")
    assert [(i["url"], i["linked_pages"]) for i in shard["items"]] == [(PAGE, [TERMS]), (TERMS, [])]

    visible = build_manifest(shard, _render(["Botox New Patient Promotion",
                                             "Receive your first Botox purchase of 20 to 300 units for $10 per unit"]))
    assert list(visible) == [PAGE]
    result = visible[PAGE]["claimResults"][0]
    assert result["status"] == "visible_match" and result["visibleOn"] == [PAGE, TERMS]

    # 条款页上没有 20 到 300 单位时，该行既不在促销页也不在条款页上，整张卡片不可见
    hidden = build_manifest(build_shard(_plan(card), "run"), _render(["Botox New Patient Promotion",
                                                                      "Receive your first Botox purchase for $10 per unit"]))
    assert hidden[PAGE]["claimResults"][0]["status"] == "not_visible"
