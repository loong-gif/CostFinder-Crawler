from __future__ import annotations

import json
import os
import re
from pathlib import Path

import requests

from utils.ego_browser import html_to_markdown

FILLER = "Our medspa offers a full range of aesthetic services for every patient. " * 5
QUOTE = "Botox 50 units Regular $450 Special $300"


def page_html(body: str, head: str = "") -> str:
    return (f"<html><head><title>Monthly Specials</title>{head}</head>"
            f"<body><main><h1>Monthly Specials</h1><p>{FILLER}</p>{body}</main></body></html>")


def local_firecrawl(site) -> str:
    """本机 Firecrawl /v1/scrape：真实请求页面，返回原始 HTML、由它转换的 Markdown 与页面状态码。"""
    session = requests.Session()
    session.trust_env = False

    def respond(body: dict):
        if "firecrawl-fails" in body["url"]:
            return 500, {"success": False, "error": "All scraping engines failed"}
        page = session.get(body["url"], timeout=10)
        title = re.search(r"<title>(.*?)</title>", page.text)
        data = {"markdown": html_to_markdown(page.text),
                "metadata": {"statusCode": page.status_code, "url": page.url, "sourceURL": body["url"],
                             "title": title.group(1) if title else ""}}
        if "rawHtml" in body["formats"]:
            data["rawHtml"] = page.text
        return 200, {"success": True, "data": data}

    return site.serve_json("firecrawl/v1/scrape", respond).removesuffix("/v1/scrape")


def local_llm(site, monkeypatch, answer: dict) -> list:
    """本机 OpenAI 兼容接口：返回页面上 offer 的提取结果，记录每次请求的消息。"""
    received: list = []

    def respond(body: dict):
        received.append(body["messages"])
        return 200, {"choices": [{"message": {"content": json.dumps(answer)}}]}

    monkeypatch.setenv("LLM_API_URL", site.serve_json("llm/chat/completions", respond))
    monkeypatch.setenv("LLM_API_KEY", "local")
    monkeypatch.setenv("LLM_MODEL", "local-extractor")
    monkeypatch.setenv("LLM_BACKEND", "openai")
    return received


def without_ego_browser(monkeypatch) -> None:
    """PATH 上没有 ego-browser：渲染一经调用就失败，并留下渲染目录。"""
    kept = [entry for entry in os.environ["PATH"].split(os.pathsep) if not (Path(entry) / "ego-browser").exists()]
    monkeypatch.setenv("PATH", os.pathsep.join(kept))


def monitor_once(state_dir: Path, firecrawl: str, *urls: str, catchup: bool = True) -> dict:
    from scripts.firecrawl_self_hosted_monitor import run_monitor

    config = {"monitors": {"daily": {"targets": [{"urls": list(urls)}]}}}
    return run_monitor(config, "daily", api_url=firecrawl, api_key="", state_dir=state_dir, timeout=10, limit=None,
                       workers=1, send_notifications=False, catchup=catchup, scan_images=True)


def snapshot_pages(state_dir: Path) -> dict:
    return json.loads((state_dir / "daily.json").read_text())


def artifact(state_dir: Path) -> dict:
    return json.loads(next((state_dir / "evidence").glob("*.json")).read_text())
