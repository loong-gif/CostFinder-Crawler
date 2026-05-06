import asyncio
import sys
import unittest
from pathlib import Path

import httpx
from bs4 import BeautifulSoup

PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from src.utils import (  # noqa: E402
    build_content_signature,
    build_llm_ready_content,
    build_segment_keys,
    build_text_segments_from_content,
    extract_page_name,
    extract_page_segments,
    fetch_sitemap_urls,
    filter_page_segments,
    normalize_domain,
    normalize_url,
    prepare_ocr_page_export,
    prepare_page_export,
    score_candidate_url,
    should_skip_url,
    should_export_page,
)


class UtilsTests(unittest.TestCase):
    def test_normalize_url_and_domain(self):
        self.assertEqual(normalize_url("example.com/pricing?utm_source=x"), "https://example.com/pricing")
        self.assertEqual(normalize_domain("https://www.example.com/pricing"), "example.com")

    def test_score_candidate_url_prefers_pricing_paths(self):
        self.assertGreater(score_candidate_url("https://example.com/pricing"), score_candidate_url("https://example.com/login"))
        self.assertLess(score_candidate_url("https://example.com/blogs/learn/botox-filler"), 0)
        self.assertTrue(should_skip_url("https://example.com/blogs/learn/botox-filler"))
        self.assertTrue(should_skip_url("https://example.com/early-anti-aging-treatments-for-your-30s"))
        self.assertTrue(should_skip_url("https://example.com/what-are-dermal-fillers"))
        self.assertTrue(should_skip_url("https://example.com/botox-vs-fillers"))
        self.assertTrue(should_skip_url("https://example.com/5-reasons-to-try-prp-treatments"))
        self.assertTrue(should_skip_url("https://example.com/offering-the-best-laser-hair-removal-in-glendale"))
        self.assertTrue(should_skip_url("https://example.com/about-us"))
        self.assertTrue(should_skip_url("https://example.com/contact"))
        self.assertTrue(should_skip_url("https://example.com/policies/shipping-policy"))

    def test_fetch_sitemap_urls_recurses_into_nested_sitemaps(self):
        responses = {
            "https://example.com/sitemap.xml": (
                200,
                b"""<sitemapindex><sitemap><loc>https://example.com/services.xml</loc></sitemap></sitemapindex>""",
            ),
            "https://example.com/sitemap_index.xml": (404, b""),
            "https://example.com/wp-sitemap.xml": (404, b""),
            "https://example.com/services.xml": (
                200,
                b"""<urlset><url><loc>https://example.com/services</loc></url><url><loc>https://example.com/membership</loc></url></urlset>""",
            ),
        }

        def handler(request: httpx.Request) -> httpx.Response:
            status, content = responses.get(str(request.url), (404, b""))
            headers = {"content-type": "application/xml"} if status == 200 else {}
            return httpx.Response(status, content=content, headers=headers, request=request)

        async def run_test():
            transport = httpx.MockTransport(handler)
            async with httpx.AsyncClient(transport=transport) as client:
                urls = await fetch_sitemap_urls("https://example.com", client=client)
            return urls

        urls = asyncio.run(run_test())
        self.assertIn("https://example.com/services", urls)
        self.assertIn("https://example.com/membership", urls)

    def test_filter_page_segments_prefers_parent_content(self):
        raw_segments = [
            {"index": 0, "tag": "section", "text": "Monthly Promotions Botox $10 Dysport $4 Save $50", "text_length": 48},
            {"index": 1, "tag": "li", "text": "Botox $10", "text_length": 9},
            {"index": 2, "tag": "li", "text": "Dysport $4", "text_length": 10},
        ]
        filtered_segments, flags = filter_page_segments(raw_segments)
        self.assertEqual(len(filtered_segments), 1)
        self.assertEqual(filtered_segments[0]["index"], 0)
        self.assertIn("drop:contained_by_parent", flags)

    def test_filter_page_segments_drops_near_duplicates_by_jaccard(self):
        raw_segments = [
            {
                "index": 0,
                "tag": "section",
                "text": "Membership Special Botox $12 per unit and filler package save $100 this month only",
                "text_length": 84,
            },
            {
                "index": 1,
                "tag": "section",
                "text": "Membership Special Botox $12 per unit with filler package save $100 this month",
                "text_length": 78,
            },
        ]
        filtered_segments, flags = filter_page_segments(raw_segments)
        self.assertEqual(len(filtered_segments), 1)
        self.assertIn("drop:jaccard_near_duplicate", flags)

    def test_prepare_page_export_drops_review_only_price_page(self):
        soup = BeautifulSoup(
            """
            <html><body>
              <section>Star star star She was wonderful, so happy, thanks Jina $450</section>
            </body></html>
            """,
            "html.parser",
        )
        payload = prepare_page_export(soup, "https://example.com/reviews")
        self.assertFalse(payload["should_export"])
        self.assertFalse(payload["page_segments_filtered"])

    def test_prepare_page_export_drops_blog_page_even_with_service_terms(self):
        soup = BeautifulSoup(
            """
            <html><head><title>Botox and Filler Recovery</title></head><body>
              <section>Botox and filler recovery timeline after treatment.</section>
              <section>United States | USD $</section>
            </body></html>
            """,
            "html.parser",
        )
        payload = prepare_page_export(soup, "https://example.com/blogs/learn/botox-and-filler-recovery")
        self.assertFalse(payload["should_export"])

    def test_prepare_page_export_keeps_service_menu(self):
        soup = BeautifulSoup(
            """
            <html><body>
              <section>
                <h2>Membership Specials</h2>
                <p>Botox $11 per unit. Save $50 this month.</p>
              </section>
            </body></html>
            """,
            "html.parser",
        )
        payload = prepare_page_export(soup, "https://example.com/pricing")
        self.assertTrue(payload["should_export"])
        self.assertIn("[SEGMENT 0]", payload["page_content"])
        self.assertIn("Botox $11 per unit", payload["page_content"])
        self.assertTrue(should_export_page(payload["page_segments_filtered"], "https://example.com/pricing"))

    def test_prepare_ocr_page_export_keeps_offer_content(self):
        ocr_text = """
        Monthly Specials

        Botox $11 per unit
        Save $50 this month
        Membership $99 per month
        """
        payload = prepare_ocr_page_export(ocr_text, "https://example.com/pricing")
        self.assertTrue(payload["should_export"])
        self.assertIn("Botox $11 per unit", payload["page_content"])

    def test_extract_page_segments_removes_menu_noise(self):
        soup = BeautifulSoup(
            """
            <html><body>
              <section>Open Menu Main Menu Botox $11 per unit Learn More</section>
            </body></html>
            """,
            "html.parser",
        )
        segments = extract_page_segments(soup)
        self.assertTrue(segments)
        self.assertIn("Botox $11 per unit", segments[0]["text"])
        self.assertNotIn("Open Menu", segments[0]["text"])

    def test_extract_page_segments_skips_hidden_nodes_and_descendants(self):
        soup = BeautifulSoup(
            """
            <html><body>
              <section style="display: none">Hidden promo Botox $5</section>
              <div class="hidden"><p>Hidden by class filler $399</p></div>
              <section hidden><div>Hidden by attribute Dysport $4</div></section>
              <section><div aria-hidden="true">Hidden child Sculptra $699</div></section>
              <section><p>Visible promo Botox $11 per unit</p></section>
            </body></html>
            """,
            "html.parser",
        )
        segments = extract_page_segments(soup)
        merged = " ".join(segment["text"] for segment in segments)
        self.assertIn("Visible promo Botox $11 per unit", merged)
        self.assertNotIn("Hidden promo Botox $5", merged)
        self.assertNotIn("filler $399", merged)
        self.assertNotIn("Dysport $4", merged)
        self.assertNotIn("Sculptra $699", merged)

    def test_build_text_segments_from_content_parses_json_array(self):
        text = '[{"text":"Botox $11 per unit"}, {"text":"Membership $99 monthly"}]'
        segments = build_text_segments_from_content(text)
        self.assertEqual(len(segments), 2)
        self.assertEqual(segments[0]["tag"], "json")
        self.assertIn("Botox $11 per unit", segments[0]["text"])

    def test_prepare_page_export_drops_short_non_offer_page(self):
        soup = BeautifulSoup("<html><body><section>Best skin care in town</section></body></html>", "html.parser")
        payload = prepare_page_export(soup, "https://example.com/service")
        self.assertFalse(payload["should_export"])

    def test_prepare_page_export_drops_long_article_without_offer_structure(self):
        soup = BeautifulSoup(
            """
            <html><body>
              <section>
                Early anti-aging treatments in your 30s are often about prevention, collagen support,
                and thoughtful self-care. Many patients choose gentle options because they want natural
                looking results and a long-term plan that fits their lifestyle and aesthetic goals.
              </section>
              <section>
                Injectable treatments and skin tightening can be discussed during a consultation, and
                the right plan depends on skin condition, lifestyle, and personal preferences rather
                than a fixed menu of special pricing.
              </section>
            </body></html>
            """,
            "html.parser",
        )
        payload = prepare_page_export(soup, "https://example.com/early-anti-aging-treatments-for-your-30s")
        self.assertFalse(payload["should_export"])

    def test_build_llm_ready_content_numbers_segments(self):
        content = build_llm_ready_content(
            [
                {"index": 3, "text": "Botox $10", "score": 8},
                {"index": 4, "text": "Membership $99/month", "score": 8},
            ]
        )
        self.assertIn("[SEGMENT 0] Botox $10", content)
        self.assertIn("[SEGMENT 1] Membership $99/month", content)

    def test_extract_page_name_prefers_og_site_name(self):
        soup = BeautifulSoup(
            """
            <html>
              <head>
                <meta property="og:site_name" content="NakedMD" />
                <title>Fallback Title</title>
              </head>
              <body><h1>Other Name</h1></body>
            </html>
            """,
            "html.parser",
        )
        self.assertEqual(extract_page_name(soup, fallback="example.com"), "NakedMD")

    def test_build_content_signature_is_stable_for_same_segments(self):
        first = [
            {"index": 0, "text": "Membership Specials Botox $11 per unit"},
            {"index": 1, "text": "Save $50 this month"},
        ]
        second = [
            {"index": 7, "text": "Membership Specials Botox $11 per unit"},
            {"index": 9, "text": "Save $50 this month"},
        ]
        self.assertEqual(build_content_signature(first), build_content_signature(second))

    def test_build_segment_keys_is_stable_for_same_text(self):
        first = [{"index": 0, "text": "Botox $11 per unit"}]
        second = [{"index": 3, "text": "Botox $11 per unit"}]
        self.assertEqual(build_segment_keys(first), build_segment_keys(second))


if __name__ == "__main__":
    unittest.main()
