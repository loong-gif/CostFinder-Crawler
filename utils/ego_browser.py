"""经 ego-lite（真实浏览器）渲染页面，转为与 Firecrawl scrape 的 data 字段同构的结果。

本机（macOS）直接调用 PATH 上的 ego-browser；monitor 主机在 WSL 中运行，调用 Windows 端的 ego-browser.exe，
要求进程以普通权限运行在已登录的桌面会话中（ego-lite 拒绝管理员权限的连接）。
"""
from __future__ import annotations

import json
import shutil
import subprocess
import threading
from typing import Any
from urllib.parse import urljoin, urlsplit

from bs4 import BeautifulSoup
from markdownify import markdownify

from utils.page_title import EGO_TASK_SPACE, PAGE_TIMEOUT_MS, is_block_page

NON_CONTENT_TAGS = ["script", "style", "noscript", "svg", "iframe", "template"]
EGO_PROCESS_TIMEOUT_S = 240
EGO_SCRIPT = """
const task = await taskSpace({space});
const page = task.page("p1");
await page.goto({url}, {{ timeout: {timeout} }});
await page.waitForTimeout(6000);
const info = await page.evaluate(() => ({{
  url: location.href,
  title: document.title,
  html: document.documentElement.outerHTML,
}}));
console.log("EGO_SCRAPE " + JSON.stringify(info));
"""
_EGO_LOCK = threading.Lock()


class BrowserScrapeFailed(Exception):
    pass


def html_to_markdown(html: str) -> str:
    soup = BeautifulSoup(html, "html.parser")
    for tag in soup(NON_CONTENT_TAGS):
        tag.decompose()
    body = soup.body or soup
    return markdownify(str(body), heading_style="ATX").strip()


def firecrawl_shaped_result(url: str, final_url: str, title: str, html: str, status: int | None,
                            engine: str) -> dict[str, Any]:
    """与 Firecrawl scrape 的 data 字段同构：markdown、rawHtml、links（绝对地址）与 metadata。"""
    markdown = html_to_markdown(html)
    if is_block_page(markdown):
        raise BrowserScrapeFailed("blocked_page")
    links = list(dict.fromkeys(urljoin(final_url, a["href"]) for a in BeautifulSoup(html, "html.parser").find_all("a", href=True)
                               if urlsplit(urljoin(final_url, a["href"])).scheme in ("http", "https")))
    return {"markdown": markdown, "html": html, "rawHtml": html, "links": links,
            "metadata": {"title": title, "sourceURL": url, "url": final_url, "statusCode": status, "engine": engine}}


def ego_command() -> str:
    """macOS 上是 ego-browser；WSL 中经 Windows 互操作调用 ego-browser.exe（首次设置把它加入了用户 PATH）。"""
    command = shutil.which("ego-browser") or shutil.which("ego-browser.exe")
    if command is None:
        raise RuntimeError("ego_browser_unavailable: ego-browser is not on PATH")
    return command


def ego_scrape(url: str) -> dict[str, Any]:
    script = EGO_SCRIPT.format(space=json.dumps(EGO_TASK_SPACE), url=json.dumps(url), timeout=PAGE_TIMEOUT_MS)
    command = ego_command()
    try:
        # 各次渲染共用同一个任务空间和页面，同一进程内只能逐个执行
        with _EGO_LOCK:
            done = subprocess.run([command, "nodejs", "-e", script], capture_output=True, text=True,
                                  encoding="utf-8", check=True, timeout=EGO_PROCESS_TIMEOUT_S)
    except subprocess.TimeoutExpired:
        # 页面卡住（例如停在验证页）是该页面的抓取结果
        raise BrowserScrapeFailed("ego_timeout") from None
    except subprocess.CalledProcessError as exc:
        # 脚本在该页面上出错（导航失败、页面崩溃）是该页面的抓取结果
        raise BrowserScrapeFailed(f"ego_script_failed: {(exc.stderr or '').strip()[-300:]}") from None
    # ego-browser 的 console 输出写到 stderr
    line = next(l for l in (done.stdout + done.stderr).splitlines() if l.startswith("EGO_SCRAPE "))
    info = json.loads(line[len("EGO_SCRAPE "):])
    return firecrawl_shaped_result(url, info["url"], info["title"], info["html"], None, "ego")
