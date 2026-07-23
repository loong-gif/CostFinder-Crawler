"""Tests for clinic_services Firecrawl search URL filtering."""
from utils.clinic_services_search import (
    SearchPage,
    business_base_domain,
    filter_service_menu_urls,
    host_matches_domain,
    is_article_service_url,
    pick_service_search_hit,
    url_path_score,
)
from utils.service_price_guard import is_catalog_ineligible_url


def test_business_base_domain():
    assert business_base_domain("dreammedspaoc.creatorlink.net") == "dreammedspaoc.creatorlink.net"
    assert business_base_domain("https://www.example.com/path") == "example.com"


def test_host_matches_domain():
    assert host_matches_domain("https://www.example.com/pricing", "example.com")
    assert not host_matches_domain("https://other.com/pricing", "example.com")


def test_url_path_score_prefers_services():
    assert url_path_score("https://example.com/services/botox") > 0
    assert url_path_score("https://example.com/specials/botox") < 0


def test_url_path_score_penalizes_article_pages():
    blog = "https://www.calistamedspa.com/masseter-botox-before-and-after-jaw-slimming-results-timeline/"
    menu = "https://www.calistamedspa.com/services/"
    assert url_path_score(menu) > url_path_score(blog)
    assert is_article_service_url(blog)
    assert not is_article_service_url(menu)


def test_pick_service_search_hit_prefers_services_menu():
    hits = [
        {
            "url": "https://www.calistamedspa.com/masseter-botox-before-and-after-jaw-slimming-results-timeline/",
            "position": 9,
            "description": "$12 per unit",
        },
        {
            "url": "https://www.calistamedspa.com/services/",
            "position": 10,
            "description": "0-49 units $13 / unit",
        },
    ]
    picked = pick_service_search_hit(hits, domain="calistamedspa.com")
    assert picked is not None
    assert picked["url"].endswith("/services/")


def test_url_path_score_penalizes_promotion_pages() -> None:
    promo = "https://laqueenmedspa.com/promotion"
    menu = "https://laqueenmedspa.com/services"
    assert url_path_score(menu) > url_path_score(promo)
    assert is_catalog_ineligible_url(promo)
    assert not is_catalog_ineligible_url(menu)


def test_filter_service_menu_urls():
    pages = [
        SearchPage("https://example.com/specials", "Specials", "x"),
        SearchPage("https://example.com/services/botox", "Services", "Botox $10/unit"),
        SearchPage("https://other.com/services", "Other", "y"),
    ]
    filtered = filter_service_menu_urls(pages, domain="example.com")
    assert len(filtered) == 1
    assert filtered[0].url.endswith("/services/botox")
