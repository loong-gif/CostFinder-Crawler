import json
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest
import requests

from utils.firecrawl_client import SelfHostedFirecrawl, scrape_page_markdown


def _firecrawl_server(blocked_fast: set[str], blocked_all: set[str]):
    """本地 HTTP 服务模拟 Firecrawl /v1/scrape：fastMode 下 blocked_fast 页面返回 500（fetch 被拦截），
    blocked_all 页面任何方式都返回 500；记录每个请求体。"""
    requests_seen: list[dict] = []

    class Handler(BaseHTTPRequestHandler):
        def do_POST(self):
            body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
            requests_seen.append(body)
            failed = body["url"] in blocked_all or (body.get("fastMode") and body["url"] in blocked_fast)
            payload = ({"success": False, "error": "All scraping engines failed"} if failed else
                       {"success": True, "data": {"markdown": f"engine={'fetch' if body.get('fastMode') else 'browser'}"}})
            raw = json.dumps(payload).encode()
            self.send_response(500 if failed else 200)
            self.send_header("Content-Length", str(len(raw)))
            self.end_headers()
            self.wfile.write(raw)

        def log_message(self, *args):
            pass

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    return server, requests_seen


def test_self_hosted_scrape_tries_fast_mode_then_the_browser() -> None:
    server, seen = _firecrawl_server(blocked_fast={"https://blocked.example/specials"},
                                     blocked_all={"https://gone.example/specials"})
    client = SelfHostedFirecrawl(f"http://127.0.0.1:{server.server_address[1]}")
    try:
        assert client.scrape("https://open.example/specials")["markdown"] == "engine=fetch"
        assert [b.get("fastMode") for b in seen] == [True]
        seen.clear()
        assert client.scrape("https://blocked.example/specials")["markdown"] == "engine=browser"
        assert [b.get("fastMode") for b in seen] == [True, None]
        assert seen[1] == {"url": "https://blocked.example/specials", "formats": ["markdown"], "onlyMainContent": True}
        with pytest.raises(requests.HTTPError):
            client.scrape("https://gone.example/specials")
    finally:
        server.shutdown()


def test_scrape_page_markdown_keeps_response_body_raw() -> None:
    class Client:
        def scrape(self, *_args, **_kwargs):
            return {"data": {"markdown": "# Title\n\n\nBody"}}

    markdown, response = scrape_page_markdown(Client(), "https://example.com/specials")

    assert markdown == "# Title\n\nBody"
    assert response["data"]["markdown"] == "# Title\n\n\nBody"
