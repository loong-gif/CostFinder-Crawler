from __future__ import annotations

import json
import socket
import time
import uuid
from urllib.parse import urlsplit
from collections import Counter
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from threading import Thread

import pytest


class CssSite:
    """本机 http.server 提供的真实页面与样式表；每个测试使用独立路径前缀，避免进程内样式表缓存互相影响。"""

    def __init__(self, base: str, files: dict, hits: Counter, posts: dict) -> None:
        self.prefix = f"/{uuid.uuid4().hex}"
        self.base = base
        self.files = files
        self.hits = hits
        self.posts = posts

    def serve_json(self, path: str, respond) -> str:
        """POST 接口：respond(请求体 JSON) 返回 (状态码, 响应 JSON)。"""
        self.posts[f"{self.prefix}/{path}"] = respond
        return self.url(path)

    def url(self, path: str) -> str:
        return f"{self.base}{self.prefix}/{path}"

    def loopback_url(self, path: str) -> str:
        """同一服务器上同一文件的回环地址。"""
        return f"http://127.0.0.1:{urlsplit(self.base).port}{self.prefix}/{path}"

    def add(self, path: str, body: str, status: int = 200, content_type: str = "text/css", delay: float = 0) -> str:
        self.files[f"{self.prefix}/{path}"] = (status, content_type, body.encode("utf-8"), delay)
        return self.url(path)

    def add_responses(self, path: str, responses: list) -> str:
        """依次返回的 (状态码, 正文)；最后一个之后一直返回最后一个。"""
        self.files[f"{self.prefix}/{path}"] = [(status, "text/css", body.encode("utf-8")) for status, body in responses]
        return self.url(path)

    def hit_count(self, path: str) -> int:
        return self.hits[f"{self.prefix}/{path}"]

    def assess(self, html: str, facts=(), markdown: str = "md", timeout: float = 15.0):
        from utils.crawler_visibility import assess_page
        from utils.crawler_visibility_fetch import fetch_stylesheets

        page_url = self.add("page.html", html, content_type="text/html")
        sheets = fetch_stylesheets(page_url, html, timeout=timeout)
        return assess_page(markdown, html, facts, page_url=page_url, stylesheets=sheets)


@pytest.fixture(scope="session")
def _css_server():
    files: dict = {}
    hits: Counter = Counter()
    posts: dict = {}

    class Handler(BaseHTTPRequestHandler):
        def do_POST(self):  # noqa: N802
            status, payload = posts[self.path](json.loads(self.rfile.read(int(self.headers["Content-Length"]))))
            body = json.dumps(payload).encode("utf-8")
            self.send_response(status)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def do_GET(self):  # noqa: N802
            hits[self.path] += 1
            entry = files.get(self.path, (404, "text/plain", b"missing"))
            if isinstance(entry, list):
                entry = entry.pop(0) if len(entry) > 1 else entry[0]
            status, content_type, body = entry[:3]
            time.sleep(entry[3] if len(entry) > 3 else 0)
            self.send_response(status)
            if 300 <= status < 400:
                self.send_header("Location", body.decode("utf-8"))  # 重定向的正文即跳转地址
            self.send_header("Content-Type", content_type)
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, *args):
            pass

    # 监听所有接口：页面经本机主机名访问（不是回环地址，utils.url_safety 放行主机名），
    # 回环地址同样可达，用来证明被安全检查拒绝的地址确实没有被请求
    server = ThreadingHTTPServer(("0.0.0.0", 0), Handler)
    Thread(target=server.serve_forever, daemon=True).start()
    yield f"http://{socket.gethostname()}:{server.server_address[1]}", files, hits, posts
    server.shutdown()


@pytest.fixture
def css_site(_css_server, monkeypatch) -> CssSite:
    # 本机测试站点不经本机的 HTTP 代理
    no_proxy = f"127.0.0.1,localhost,{socket.gethostname()}"
    monkeypatch.setenv("NO_PROXY", no_proxy)
    monkeypatch.setenv("no_proxy", no_proxy)
    base, files, hits, posts = _css_server
    return CssSite(base, files, hits, posts)
