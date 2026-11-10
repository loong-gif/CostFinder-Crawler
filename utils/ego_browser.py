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

from utils.firecrawl_client import GONE_STATUSES
from utils.page_title import PAGE_TIMEOUT_MS, PageGone, TitleUnavailable, choose_title, is_block_page

EGO_TASK_SPACE = "costfinder page titles"

NON_CONTENT_TAGS = ["script", "style", "noscript", "svg", "iframe", "template"]
EGO_PROCESS_TIMEOUT_S = 240
# 网站请求定位等浏览器权限时，ego-lite 弹出提示并把整个任务空间交给用户，此后该空间的所有脚本都失败。
# 打开网站之前经页面拒绝这些权限：不带 origin 的设置作用于所有网站，被拒绝的请求不再弹出提示
# （task.cdp 不支持 Browser.setPermission）。三个 ego 脚本（抓取、标题、监控渲染桥接）共用这一段。
DENY_PERMISSIONS_JS = """
for (const name of ["geolocation", "notifications", "camera", "microphone", "midi", "clipboard-read",
                    "display-capture", "local-fonts", "window-management", "storage-access"]) {
  await page.cdp("Browser.setPermission", { permission: { name }, setting: "denied" });
}
"""
EGO_SCRIPT = """
const task = await taskSpace({space});
const page = task.page("p1");
{deny}
await page.goto({url}, {{ timeout: {timeout} }});
await page.waitForTimeout(6000);
const info = await page.evaluate(() => ({{
  url: location.href,
  title: document.title,
  html: document.documentElement.outerHTML,
}}));
const text = JSON.stringify(info).replace(/[\\u007f-\\uffff]/g, c => "\\\\u" + c.charCodeAt(0).toString(16).padStart(4, "0"));
const count = Math.ceil(text.length / {chunk});
for (let i = 0; i < count; i++) {{
  console.log(`EGO_CHUNK ${{i}} ${{count}} ${{text.length}} ` + text.slice(i * {chunk}, (i + 1) * {chunk}));
}}
"""
# 整页结果按块输出：ego-browser 的其他输出行可能插进单个超长行的中间，把结果拆断
EGO_CHUNK_CHARS = 4000
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


def chunked_result(output: str) -> dict[str, Any]:
    """按序号拼回 EGO_CHUNK 行；块缺失或长度不符是该页面的抓取失败。只按 \\n 分行。"""
    chunks: dict[int, str] = {}
    shape: set[tuple[int, int]] = set()
    for line in output.split("\n"):
        if line.startswith("EGO_CHUNK "):
            _, index, count, total, part = line.split(" ", 4)
            chunks[int(index)] = part
            shape.add((int(count), int(total)))
    if len(shape) != 1:
        raise BrowserScrapeFailed(f"ego_output_incomplete: {len(chunks)} chunks")
    count, total = shape.pop()
    text = "".join(chunks.get(i, "") for i in range(count))
    if sorted(chunks) != list(range(count)) or len(text) != total:
        raise BrowserScrapeFailed(f"ego_output_incomplete: {len(chunks)}/{count} chunks, {len(text)}/{total} chars")
    return json.loads(text)


def ego_scrape(url: str) -> dict[str, Any]:
    script = EGO_SCRIPT.format(space=json.dumps(EGO_TASK_SPACE), url=json.dumps(url), timeout=PAGE_TIMEOUT_MS,
                               deny=DENY_PERMISSIONS_JS,
                               chunk=EGO_CHUNK_CHARS)
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
    info = chunked_result(done.stdout + done.stderr)
    return firecrawl_shaped_result(url, info["url"], info["title"], info["html"], None, "ego")


EGO_TITLE_SCRIPT = """
const task = await taskSpace({space});
const page = task.page("p1");
{deny}
await page.goto({url}, {{ timeout: {timeout} }});
await page.waitForTimeout(6000);
const info = await page.evaluate(() => ({{
  title: document.title,
  og: document.querySelector('meta[property="og:title"]')?.content || "",
  h1: document.querySelector("h1")?.innerText || "",
  status: performance.getEntriesByType("navigation")[0]?.responseStatus ?? null,
}}));
console.log("EGO_TITLE " + JSON.stringify(info));
"""


def fetch_page_title(url: str, business_name: str = "") -> str:
    """抓取结果里没有可用标题时，经 ego-lite 渲染页面取 <title> → og:title → 第一个 <h1>。

    导航失败、页面卡住与没有可用标题都是该页面的结果，记为 TitleUnavailable。
    """
    script = EGO_TITLE_SCRIPT.format(space=json.dumps(EGO_TASK_SPACE), url=json.dumps(url), timeout=PAGE_TIMEOUT_MS,
                                     deny=DENY_PERMISSIONS_JS)
    command = ego_command()
    try:
        # 与 ego_scrape 共用同一个任务空间和页面
        with _EGO_LOCK:
            done = subprocess.run([command, "nodejs", "-e", script], capture_output=True, text=True,
                                  encoding="utf-8", check=True, timeout=EGO_PROCESS_TIMEOUT_S)
    except subprocess.TimeoutExpired:
        raise TitleUnavailable("ego_timeout") from None
    except subprocess.CalledProcessError as exc:
        raise TitleUnavailable(f"navigation_failed: {(exc.stderr or '').strip()[-200:]}") from None
    # 只按 \n 分行：JSON.stringify 不转义 U+2028/U+2029，splitlines() 会把标题中间截断
    line = next(l for l in (done.stdout + done.stderr).split("\n") if l.startswith("EGO_TITLE "))
    info = json.loads(line[len("EGO_TITLE "):])
    if info.get("status") in GONE_STATUSES:
        raise PageGone("page_gone")
    return choose_title([info["title"], info["og"], info["h1"]], business_name)
