from utils.firecrawl_client import scrape_page_markdown


def test_scrape_page_markdown_keeps_response_body_raw() -> None:
    class Client:
        def scrape(self, *_args, **_kwargs):
            return {"data": {"markdown": "# Title\n\n\nBody"}}

    markdown, response = scrape_page_markdown(Client(), "https://example.com/specials")

    assert markdown == "# Title\n\nBody"
    assert response["data"]["markdown"] == "# Title\n\n\nBody"
