from __future__ import annotations

import json
from http.server import BaseHTTPRequestHandler, HTTPServer
from threading import Thread

import pytest

from utils.browser_scrape import scrapling_scrape
from utils.ego_browser import BrowserScrapeFailed, ego_command, ego_scrape, html_to_markdown
from utils.page_title import is_block_page


def test_ego_script_error_on_a_page_is_that_page_failure(tmp_path, monkeypatch):
    # PATH 上放一个失败的 ego-browser：脚本在页面上出错时 crawl 记录该页面失败并继续其他页面
    fake = tmp_path / "ego-browser"
    fake.write_text("#!/bin/sh\necho 'Error: page crashed' >&2\nexit 1\n")
    fake.chmod(0o755)
    monkeypatch.setenv("PATH", f"{tmp_path}:/usr/bin:/bin")
    with pytest.raises(BrowserScrapeFailed, match="ego_script_failed: Error: page crashed"):
        ego_scrape("https://glow.example/specials")


def _ego_chunks(info: dict, size: int = 4000) -> list[str]:
    # ego 脚本的输出：纯 ASCII 的 JSON 按固定长度分块，每块一行，带序号、块数与总长度
    text = json.dumps(info, ensure_ascii=True)
    parts = [text[i:i + size] for i in range(0, len(text), size)]
    return [f"EGO_CHUNK {i} {len(parts)} {len(text)} {part}" for i, part in enumerate(parts)]


def _fake_ego(tmp_path, monkeypatch, lines: list[str]) -> None:
    payload = tmp_path / "payload.txt"
    payload.write_text("\n".join(lines) + "\n", encoding="utf-8")
    fake = tmp_path / "ego-browser"
    fake.write_text(f"#!/bin/sh\ncat '{payload}' >&2\n")
    fake.chmod(0o755)
    monkeypatch.setenv("PATH", f"{tmp_path}:/usr/bin:/bin")


def test_ego_result_split_by_other_output_lines_is_read_whole(tmp_path, monkeypatch):
    # ego-browser 的其他输出行插在结果中间时，按序号拼回整页；非 ASCII 字符原样保留
    info = {"url": "https://glow.example/specials", "title": "Specials",
            "html": "<html><body><h2>Botox \U0001F389</h2>" + "<p>x</p>" * 1500 + "<p>Filler $500</p></body></html>"}
    chunks = _ego_chunks(info)
    assert len(chunks) >= 3
    _fake_ego(tmp_path, monkeypatch, [chunks[0], "warning: task space reconnected", *chunks[1:]])
    result = ego_scrape("https://glow.example/specials")
    assert "Filler $500" in result["markdown"] and "\U0001F389" in result["markdown"]
    assert result["metadata"]["title"] == "Specials"


def test_ego_result_with_a_cut_or_missing_chunk_is_that_page_failure(tmp_path, monkeypatch):
    # 某一块被截断或缺失时，记为该页面的抓取失败，不作为未分类的 JSON 错误抛出
    info = {"url": "https://glow.example/specials", "title": "Specials",
            "html": "<html><body>" + "<p>x</p>" * 1500 + "</body></html>"}
    chunks = _ego_chunks(info)
    _fake_ego(tmp_path, monkeypatch, [chunks[0], chunks[1][:300], "noise", *chunks[2:]])
    with pytest.raises(BrowserScrapeFailed, match="ego_output_incomplete"):
        ego_scrape("https://glow.example/specials")
    _fake_ego(tmp_path, monkeypatch, [chunks[0], *chunks[2:]])
    with pytest.raises(BrowserScrapeFailed, match="ego_output_incomplete"):
        ego_scrape("https://glow.example/specials")


def test_ego_result_with_a_unicode_line_separator_in_the_page_is_read_whole(tmp_path, monkeypatch):
    # 页面中的 U+2028 不会把结果拆开
    info = {"url": "https://glow.example/specials", "title": "Specials",
            "html": "<html><body><h2>Botox $10/unit this week</h2><p>Filler $500</p></body></html>"}
    _fake_ego(tmp_path, monkeypatch, _ego_chunks(info))
    result = ego_scrape("https://glow.example/specials")
    assert "Filler $500" in result["markdown"] and result["metadata"]["title"] == "Specials"


def test_title_fetch_uses_ego_and_a_navigation_failure_is_that_page_result(tmp_path, monkeypatch):
    from utils.ego_browser import fetch_page_title
    from utils.page_title import TitleUnavailable

    fake = tmp_path / "ego-browser"
    monkeypatch.setenv("PATH", f"{tmp_path}:/usr/bin:/bin")
    fake.write_text("#!/bin/sh\necho 'EGO_TITLE {\"title\": \"Specials | Glow Spa\", \"og\": \"\", \"h1\": \"\"}' >&2\n")
    fake.chmod(0o755)
    assert fetch_page_title("https://glow.example/specials", "Glow Spa") == "Specials | Glow Spa"
    fake.write_text("#!/bin/sh\necho 'Error: net::ERR_NAME_NOT_RESOLVED' >&2\nexit 1\n")
    with pytest.raises(TitleUnavailable, match="navigation_failed"):
        fetch_page_title("https://gone.example/specials", "Gone Spa")


def test_title_fetch_of_a_404_page_raises_page_gone(tmp_path, monkeypatch):
    from utils.ego_browser import fetch_page_title
    from utils.page_title import PageGone

    fake = tmp_path / "ego-browser"
    monkeypatch.setenv("PATH", f"{tmp_path}:/usr/bin:/bin")
    fake.write_text("#!/bin/sh\necho 'EGO_TITLE {\"title\": \"404 Not Found\", \"og\": \"\", \"h1\": \"\", "
                    "\"status\": 404}' >&2\n")
    fake.chmod(0o755)
    with pytest.raises(PageGone, match="page_gone"):
        fetch_page_title("https://fixmedspa.com/specials", "Fix Med Spa")


def test_ego_command_uses_the_windows_executable_under_wsl(tmp_path, monkeypatch):
    # WSL 中 PATH 上只有 Windows 端的 ego-browser.exe（首次设置写入的用户 PATH）
    exe = tmp_path / "ego-browser.exe"
    exe.write_text("#!/bin/sh\n")
    exe.chmod(0o755)
    monkeypatch.setenv("PATH", str(tmp_path))
    assert ego_command() == str(exe)


def test_missing_ego_browser_stops_the_run(tmp_path, monkeypatch):
    monkeypatch.setenv("PATH", str(tmp_path))
    with pytest.raises(RuntimeError, match="ego_browser_unavailable"):
        ego_scrape("https://glow.example/specials")


@pytest.fixture
def local_site():
    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):
            if self.path == "/specials":
                body = b"<html><head><title>Specials</title></head><body><h2>Botox $10/unit</h2></body></html>"
                self.send_response(200)
            else:
                body = b"gone"
                self.send_response(404)
            self.send_header("Content-Type", "text/html")
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, *args):
            pass

    server = HTTPServer(("127.0.0.1", 0), Handler)
    Thread(target=server.serve_forever, daemon=True).start()
    yield f"http://127.0.0.1:{server.server_port}"
    server.shutdown()


def test_scrapling_scrape_returns_firecrawl_shaped_result(local_site, monkeypatch):
    monkeypatch.setenv("NO_PROXY", "127.0.0.1")
    result = scrapling_scrape(f"{local_site}/specials")
    assert result["markdown"] == "## Botox $10/unit"
    assert result["metadata"]["title"] == "Specials" and result["metadata"]["engine"] == "scrapling"


def test_scrapling_non_200_is_that_page_failure(local_site, monkeypatch):
    monkeypatch.setenv("NO_PROXY", "127.0.0.1")
    with pytest.raises(BrowserScrapeFailed, match="scrapling_status_404"):
        scrapling_scrape(f"{local_site}/missing")


def test_scrapling_connection_failure_is_that_page_failure():
    with pytest.raises(BrowserScrapeFailed, match="scrapling_request_failed"):
        scrapling_scrape("https://127.0.0.1:1/specials")


def test_block_page_served_with_200_is_detected():
    assert is_block_page("# 402\n\n### Access Denied !\n\nYou are not authorized person to access this site.")
    assert is_block_page("## Just a moment...\n\nChecking your browser")


def test_promotion_page_is_not_block_page():
    assert not is_block_page("# Monthly Specials\n\n## Botox $10/unit\n\nValid through November.")


def test_empty_heading_in_page_chrome_does_not_make_a_promotion_page_a_block_page():
    assert not is_block_page("## Just added to your cart\n\n##\n\n## Specials\n\nBotox $10/unit")
    assert is_block_page("##\n\n## Just a moment...\n\nChecking your browser")


def test_html_to_markdown_drops_scripts_and_keeps_text():
    html = "<html><head><style>p{}</style></head><body><script>x()</script><h2>Specials</h2><p>Botox $10</p></body></html>"
    assert html_to_markdown(html) == "## Specials\n\nBotox $10"
