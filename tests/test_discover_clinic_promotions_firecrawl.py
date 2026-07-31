from scripts.discover_clinic_promotions_firecrawl import (
    discover_promotion_urls,
    map_links_to_urls,
)


def test_map_links_to_urls_supports_sdk_link_objects_and_dicts() -> None:
    class Link:
        url = "https://example.com/specials"

    assert map_links_to_urls([Link(), {"url": "https://example.com/promo/summer"}, "https://example.com/"]) == [
        "https://example.com/specials",
        "https://example.com/promo/summer",
        "https://example.com/",
    ]


def test_discover_promotion_urls_keeps_only_same_site_special_or_promo_paths() -> None:
    urls = discover_promotion_urls(
        "https://www.example.com",
        [
            "https://example.com/specials/",
            "https://www.example.com/promo/summer?utm=1",
            "https://example.com/services/botox",
            "https://other.example.com/promo",
            "https://evil.example.org/special",
            "not a url",
        ],
    )

    assert urls == [
        "https://example.com/specials",
        "https://example.com/promo/summer",
    ]
