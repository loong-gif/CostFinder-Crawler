import json
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest

from utils.firecrawl_client import PageGone, SelfHostedFirecrawl, scrape_page_markdown

CLOUDFLARE_BLOCK = ("Please enable cookies.\n\nSorry, you have been blocked\n============================\n\n"
                    "You are unable to access laseraway.com\n--------------------------------------\n")


def _firecrawl_server(responses: dict[str, tuple[int, dict]]):
    """本地 HTTP 服务模拟 Firecrawl /v1/scrape：按 url 返回预设的 (HTTP 状态, 响应体)，记录每个请求体。"""
    requests_seen: list[dict] = []

    class Handler(BaseHTTPRequestHandler):
        def do_POST(self):
            body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
            requests_seen.append(body)
            status, payload = responses[body["url"]]
            raw = json.dumps(payload).encode()
            self.send_response(status)
            self.send_header("Content-Length", str(len(raw)))
            self.end_headers()
            self.wfile.write(raw)

        def log_message(self, *args):
            pass

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    return server, requests_seen


def _page(markdown: str, status: int = 200) -> tuple[int, dict]:
    return 200, {"success": True, "data": {"markdown": markdown, "metadata": {"statusCode": status}}}


FAILED = (500, {"success": False, "error": "All scraping engines failed"})
TIMEOUT = (408, {"success": False, "code": "SCRAPE_TIMEOUT"})


def _client(server, rendered: list[str]) -> SelfHostedFirecrawl:
    def ego(url: str) -> dict:
        rendered.append(url)
        return {"markdown": "# Promotions\n\n### Your First Botox Treatment Is $10 Per Unit", "metadata": {"engine": "ego"}}

    return SelfHostedFirecrawl(f"http://127.0.0.1:{server.server_address[1]}", browser=ego)


def test_fast_mode_content_is_used_without_a_browser() -> None:
    server, seen = _firecrawl_server({"https://open.example/specials": _page("engine=fetch")})
    rendered: list[str] = []
    try:
        assert _client(server, rendered).scrape("https://open.example/specials")["markdown"] == "engine=fetch"
        assert seen == [{"url": "https://open.example/specials", "formats": ["markdown"], "onlyMainContent": True,
                         "fastMode": True}]
        assert rendered == []
    finally:
        server.shutdown()


def test_pages_fast_mode_cannot_fetch_are_rendered_by_ego_without_the_firecrawl_browser() -> None:
    server, seen = _firecrawl_server({
        "https://www.laseraway.com/promotions/": _page(CLOUDFLARE_BLOCK, status=403),
        "https://cf.example/specials": _page(CLOUDFLARE_BLOCK),
        "https://broken.example/specials": FAILED,
        "https://slow.example/specials": TIMEOUT,
        "https://script.example/specials": _page("   "),
    })
    rendered: list[str] = []
    client = _client(server, rendered)
    try:
        for url in ("https://www.laseraway.com/promotions/", "https://cf.example/specials",
                    "https://broken.example/specials", "https://slow.example/specials", "https://script.example/specials"):
            assert client.scrape(url)["metadata"]["engine"] == "ego"
        assert rendered == [b["url"] for b in seen]
        # 每个页面只向 Firecrawl 发一次 fastMode 请求，不再请求浏览器引擎
        assert all(b["fastMode"] is True for b in seen)
    finally:
        server.shutdown()


def test_a_page_that_no_longer_exists_is_that_page_result() -> None:
    server, _ = _firecrawl_server({
        "https://gone.example/specials": _page("# Page not found", status=404),
        "https://removed.example/specials": _page("# Gone", status=410),
    })
    rendered: list[str] = []
    client = _client(server, rendered)
    try:
        with pytest.raises(PageGone, match="page_gone_404"):
            client.scrape("https://gone.example/specials")
        with pytest.raises(PageGone, match="page_gone_410"):
            client.scrape("https://removed.example/specials")
        assert rendered == []
    finally:
        server.shutdown()


def test_scrape_page_markdown_keeps_response_body_raw() -> None:
    class Client:
        def scrape(self, *_args, **_kwargs):
            return {"data": {"markdown": "# Title\n\n\nBody"}}

    markdown, response = scrape_page_markdown(Client(), "https://example.com/specials")

    assert markdown == "# Title\n\nBody"
    assert response["data"]["markdown"] == "# Title\n\n\nBody"
