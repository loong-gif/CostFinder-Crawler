#!/usr/bin/env python3
"""Crawl stubborn URLs with Playwright (headless browser)."""

import csv
import re
import time

try:
    from playwright.sync_api import sync_playwright
except ImportError as playwright_import_error:
    sync_playwright = None
    _PLAYWRIGHT_IMPORT_ERROR = playwright_import_error
else:
    _PLAYWRIGHT_IMPORT_ERROR = None

OUTPUT_CSV = "/Users/wyl/costfinder/scripts/crawl_results.csv"

URLS = [
    "https://rekindlebeauty.com/tucson-med-spa-broadway/",
    "https://www.idealimage.com",
    "http://rsalonaz.com/",
    "http://www.facethefight.com/",
    "https://renewedmedicalhealth.com/",
]

def clean_text(text):
    """Extract meaningful text, remove excess whitespace."""
    # Remove script/style content
    text = re.sub(r'<script[^>]*>.*?</script>', '', text, flags=re.DOTALL | re.IGNORECASE)
    text = re.sub(r'<style[^>]*>.*?</style>', '', text, flags=re.DOTALL | re.IGNORECASE)
    # Remove HTML tags
    text = re.sub(r'<[^>]+>', ' ', text)
    # Decode entities
    text = text.replace('&amp;', '&').replace('&lt;', '<').replace('&gt;', '>').replace('&nbsp;', ' ')
    # Collapse whitespace
    text = re.sub(r'\s+', ' ', text).strip()
    return text

results = []

if sync_playwright is None:
    raise SystemExit(
        "缺少可选浏览器依赖。请先执行 "
        "`uv pip install -r requirements_browser_tools.txt`，再执行 "
        "`PLAYWRIGHT_BROWSERS_PATH=.playwright_browsers playwright install chromium`。"
    ) from _PLAYWRIGHT_IMPORT_ERROR

with sync_playwright() as p:
    browser = p.chromium.launch(headless=True)
    context = browser.new_context(
        user_agent="Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
        viewport={"width": 1280, "height": 800}
    )
    
    for url in URLS:
        print(f"\n=== {url} ===", flush=True)
        page = context.new_page()
        try:
            # Navigate with extended timeout
            page.goto(url, wait_until="networkidle", timeout=30000)
            time.sleep(2)  # extra wait for JS rendering
            
            title = page.title()
            print(f"  Title: {title[:80]}", flush=True)
            
            # Get rendered text content
            body_text = page.evaluate("() => document.body ? document.body.innerText : ''")
            body_text = body_text.strip()
            print(f"  Body text: {len(body_text)} chars", flush=True)
            
            if len(body_text) > 50:
                # Truncate to reasonable size
                if len(body_text) > 5000:
                    body_text = body_text[:5000] + "... [truncated]"
                results.append({
                    "source_url": url,
                    "subpage_url": url,
                    "page_content": body_text,
                    "domain": url.split("//")[1].split("/")[0],
                    "name": title,
                    "error": ""
                })
                print(f"  ✓ Content captured", flush=True)
            else:
                # Try screenshot + visible text selectors
                all_text = page.evaluate("""() => {
                    const selectors = ['main', 'article', '[role="main"]', '#content', '.content', '#app', '#root'];
                    for (const sel of selectors) {
                        const el = document.querySelector(sel);
                        if (el && el.innerText.trim().length > 50) return el.innerText.trim();
                    }
                    return document.body ? document.body.innerHTML : '';
                }""")
                cleaned = clean_text(all_text)
                if len(cleaned) > 50:
                    if len(cleaned) > 5000:
                        cleaned = cleaned[:5000] + "... [truncated]"
                    results.append({
                        "source_url": url,
                        "subpage_url": url,
                        "page_content": cleaned,
                        "domain": url.split("//")[1].split("/")[0],
                        "name": title,
                        "error": ""
                    })
                    print(f"  ✓ Content from selectors ({len(cleaned)} chars)", flush=True)
                else:
                    results.append({
                        "source_url": url,
                        "subpage_url": "",
                        "page_content": "",
                        "domain": url.split("//")[1].split("/")[0],
                        "name": title,
                        "error": "playwright_no_content"
                    })
                    print(f"  ✗ Still no content", flush=True)
        except Exception as e:
            results.append({
                "source_url": url,
                "subpage_url": "",
                "page_content": "",
                "domain": url.split("//")[1].split("/")[0],
                "name": "",
                "error": f"playwright_error: {str(e)[:100]}"
            })
            print(f"  ✗ Error: {e}", flush=True)
        finally:
            page.close()
    
    browser.close()

# Update CSV: remove old rows for these URLs, add new ones
with open(OUTPUT_CSV) as f:
    existing = list(csv.DictReader(f))

retry_urls = set(URLS)
filtered = [r for r in existing if r["source_url"] not in retry_urls]
filtered.extend(results)

fieldnames = ["source_url", "subpage_url", "page_content", "domain", "name", "error"]
with open(OUTPUT_CSV, "w", newline="", encoding="utf-8") as f:
    writer = csv.DictWriter(f, fieldnames=fieldnames, extrasaction="ignore")
    writer.writeheader()
    writer.writerows(filtered)

has_content = len([r for r in filtered if r.get("subpage_url")])
print(f"\nCSV updated: {len(filtered)} rows, {has_content} with content", flush=True)
