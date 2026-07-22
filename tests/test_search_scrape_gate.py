from utils.clinic_services_search import SearchPage
from utils.search_scrape_gate import search_hit_has_price, search_page_has_price


def test_search_page_requires_price_in_hit() -> None:
    assert search_page_has_price(
        SearchPage(url="https://example.com/membership/", title="Membership", markdown="$60/month")
    )
    assert not search_page_has_price(
        SearchPage(url="https://example.com/membership/", title="Membership", markdown="Join today")
    )


def test_search_hit_uses_description() -> None:
    assert search_hit_has_price(description="Special: Hydrafacial from $199")
    assert not search_hit_has_price(description="Learn about our wellness programs")
