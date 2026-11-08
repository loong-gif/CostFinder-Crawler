"""Rendered evidence from ego-lite at the desktop (PC) viewport.

WSL 1 cannot run the container Chromium processes reliably. Each render starts
scripts/monitor_ego_bridge.js inside ego-lite, which relays CDP commands sent to
it over loopback HTTP to the single page of one render slot's task space.
"""
from __future__ import annotations

import base64
import hashlib
import re
import time
from pathlib import Path
from urllib.parse import urlsplit

from utils.monitor_price_store import atomic_bytes, atomic_json, digest, now
from utils.monitor_image_evidence import tile_ranges, pixel_digest
from utils.monitor_rendered_cdp import ROOT, ego_cdp_page, frame_regions
from utils.monitor_rendered_checks import promotion_images, resources_complete, text_has_promotion
from utils.monitor_rendered_variants import (
    _dedupe_variant_captures, _product_variant_reconciliation, capture_product_variants)

# 只渲染 PC 宽度；覆盖检查（写入策略门、计划）按这里的名称集合判定完整
BREAKPOINTS = (("desktop", 1440, False),)
BREAKPOINT_NAMES = frozenset(name for name, _, _ in BREAKPOINTS)


PRODUCT_LINKS = """(()=>{
 const visible=el=>{for(let p=el;p;p=p.parentElement){const s=getComputedStyle(p);if(s.display==='none'||s.visibility==='hidden'||s.visibility==='collapse'||Number(s.opacity)===0||p.hidden)return false;}return [...el.getClientRects()].some(r=>r.width>0&&r.height>0);};
 return [...new Set([...document.querySelectorAll('main a[href*="/products/"], [role="main"] a[href*="/products/"]')]
  .filter(visible).map(a=>new URL(a.href,location.href)).filter(u=>u.origin===location.origin&&u.pathname.includes('/products/')).map(u=>u.origin+u.pathname))];
})()"""


def combine_breakpoint_reports(url: str, reports: list[dict]) -> dict:
    """Only a complete union of every BREAKPOINTS viewport can establish absence."""
    good = [row for row in reports if row.get("accessible")]
    base = dict(good[0] if good else reports[0])
    coverage = {row["breakpoint"]: {"width": row["width"], "complete": row.get("complete") is True,
                 "accessible": row.get("accessible") is True, "captured_at": row.get("captured_at"),
                 "screenshot_path": row.get("screenshot_path"), "failures": row.get("failures", [])}
                for row in reports}
    complete = set(coverage) == BREAKPOINT_NAMES and all(v["complete"] for v in coverage.values())
    texts = list(dict.fromkeys(row.get("text", "") for row in good if row.get("text")))
    tiles = {round_name: [tile for row in reports for tile in row.get("image_tiles", {}).get(round_name, [])]
             for round_name in ("a", "b")}
    base.update(url=url, text="\n".join(texts), accessible=bool(good), complete=complete,
        breakpoint_coverage=coverage, frames=[frame for row in reports for frame in row.get("frames", [])],
        failures=list(dict.fromkeys(failure for row in reports for failure in row.get("failures", []))),
        image_review_required=any(row.get("image_review_required") for row in reports), image_tiles=tiles,
        visual_digest=digest([row.get("visual_digest") for row in reports]), context_digest=digest(texts),
        image_seconds_remaining=min((row.get("image_seconds_remaining", 300) for row in good), default=0),
        scroll_complete=all(row.get("scroll_complete") for row in reports),
        product_links=list(dict.fromkeys(link for row in good for link in row.get("product_links", []))),
        variant_captures=[capture for row in good for capture in row.get("variant_captures", [])],
        variant_expected=max((row.get("variant_expected", 0) for row in good), default=0),
        variant_skipped=[skip for row in good for skip in row.get("variant_skipped", [])],
        variants_complete=all(row.get("variants_complete", True) for row in reports))
    return base


def render_page(url: str, directory: Path, *, force_ocr: bool = False, product_details: bool = True,
                business_id: int | None = None) -> dict:
    reports = []
    for name, width, mobile in BREAKPOINTS:
        try:
            report = _render_page(url, directory / name, force_ocr=force_ocr,
                                  width=width, mobile=mobile, collect_variants="/products/" in urlsplit(url).path)
        except Exception as exc:
            report = {"url": url, "accessible": False, "complete": False, "failures": [type(exc).__name__],
                      "error": str(exc)[:500]}
        reports.append({**report, "breakpoint": name, "width": width})
    result = combine_breakpoint_reports(url, reports)
    if product_details and "/collections/" in urlsplit(url).path:
        result["product_details"] = []
        for index, link in enumerate(result["product_links"]):
            detail = render_page(link, directory / "products" / str(index), force_ocr=force_ocr,
                                 product_details=False, business_id=business_id)
            result["product_details"].append(detail)
        result["product_variant_reconciliation"] = [_product_variant_reconciliation(d) for d in result["product_details"]]
        result["variants_complete"] = bool(result["product_links"]) and all(
            detail.get("complete") and detail.get("variants_complete") and detail.get("variant_captures")
            for detail in result["product_details"])
        result["complete"] = result["complete"] and result["variants_complete"]
        result["text"] += "\n" + "\n".join(detail.get("text", "") for detail in result["product_details"])
        result["image_review_required"] = result["image_review_required"] or any(d.get("image_review_required") for d in result["product_details"])
        for round_name in ("a", "b"):
            result["image_tiles"][round_name].extend(tile for detail in result["product_details"] for tile in detail.get("image_tiles", {}).get(round_name, []))
    variant_captures = [c for d in result.get("product_details", []) for c in d.get("variant_captures", [])] or result.get("variant_captures", [])
    variant_captures = _dedupe_variant_captures(variant_captures, business_id)
    result["variant_captures"] = variant_captures
    if not result.get("product_variant_reconciliation") and result.get("variant_expected") is not None:
        result["product_variant_reconciliation"] = [_product_variant_reconciliation(result)]
    for capture in variant_captures:
        for round_name, field in (("a", "screenshot_path"), ("b", "second_screenshot_path")):
            path = capture.get(field)
            if path:
                data = Path(path).read_bytes()
                tile = {"path":path,"bytes":len(data),"png_digest":hashlib.sha256(data).hexdigest()}
                if round_name == "a":
                    tile["pixel_digest"] = pixel_digest(data)
                result["image_tiles"][round_name].append(tile)
    if variant_captures:
        result["visual_digest"] = digest({"old":result["visual_digest"],"variants":[t["png_digest"] for t in result["image_tiles"]["b"]]})
        result["context_digest"] = digest({"text":result["text"],"variants":[c["offer_raw_text"] for c in variant_captures]})
        result["image_review_required"] = True
    atomic_json(directory / "browser.json", result)
    return result


def _render_page(url: str, directory: Path, *, force_ocr: bool = False, width: int = 1440, mobile: bool = False, collect_variants: bool = False) -> dict:
    started = time.monotonic()
    with ego_cdp_page(started+300) as page:
        page.call("Page.enable")
        page.call("Network.enable")
        page.call("Emulation.setDeviceMetricsOverride",width=width,height=1600,deviceScaleFactor=1,mobile=mobile)
        navigation = page.call("Page.navigate",url=url)
        if navigation.get("errorText"):
            raise RuntimeError("rendered_page_failed")
        page.wait_loaded()
        stable = page.evaluate("""(async()=>{
          const end=Date.now()+55000;
          while(document.readyState!=='complete' && Date.now()<end) await new Promise(r=>setTimeout(r,200));
          if(document.readyState!=='complete') return false;
          let y=0, previous=-1;
          while(Date.now()<end) {
            const height=document.documentElement.scrollHeight;
            window.scrollTo(0,y); await new Promise(r=>setTimeout(r,160));
            if(y+innerHeight>=height) {
              if(height===previous) {window.scrollTo(0,0); return true;}
              previous=height;await new Promise(r=>setTimeout(r,500));
            } else y+=800;
          }
          window.scrollTo(0,0); return false;
        })()""")
        page.call("Runtime.enable")
        # This call drains executionContextCreated events before inspection.
        frame_tree = page.call("Page.getFrameTree")["frameTree"]
        contexts = {event["params"]["context"]["auxData"]["frameId"]:event["params"]["context"]["id"]
                    for event in page.events if event.get("method") == "Runtime.executionContextCreated"
                    and event["params"]["context"].get("auxData",{}).get("isDefault")}
        scan = (ROOT / "scripts/monitor_rendered_page.js").read_text()
        scans,failures = [],[]
        frame = frame_tree["frame"]
        context = contexts.get(frame["id"])
        if context is None:
            failures.append("frame_context_unavailable")
        else:
            try:
                scans.append({**page.evaluate(scan+"()",context),"url":frame["url"]})
            except RuntimeError:
                failures.append("frame_scan_failed")
        embedded = frame_regions(page, frame_tree)
        if embedded:
            failures.append("embedded_region_partial")
            if scans: scans[0]["regions"].extend(embedded)
        title = page.evaluate("document.title")
        final_url = page.evaluate("location.href")
        metrics = page.call("Page.getLayoutMetrics")["cssContentSize"]
        height = min(metrics["height"],32000)
        if height < metrics["height"]:
            failures.append("screenshot_truncated")
        capture = page.call("Page.captureScreenshot",format="png",captureBeyondViewport=True,
                            clip={"x":0,"y":0,"width":width,"height":height,"scale":1})
        atomic_bytes(directory / "screenshot.png",base64.b64decode(capture["data"],validate=True))
        request_urls = {event["params"]["requestId"]:event["params"]["request"]["url"]
                        for event in page.events if event.get("method") == "Network.requestWillBeSent"}
        failed_urls = {request_urls.get(event["params"]["requestId"])
                       for event in page.events if event.get("method") == "Network.loadingFailed"}
        failed_urls.update(event["params"]["response"]["url"] for event in page.events
                           if event.get("method") == "Network.responseReceived" and event["params"]["response"]["status"] >= 400)
        for frame in scans:
            for image in frame.get("backgrounds",[]):
                image["network_failed"] = image["src"] in failed_urls
        dom_text = "\n".join(frame["text"] for frame in scans)
        text = "" if any(f.get("obscured") for f in scans) else dom_text
        responses = [event["params"]["response"] for event in page.events if event.get("method") == "Network.responseReceived"
                     and event["params"].get("type") == "Document" and event["params"].get("frameId") == frame_tree["frame"]["id"]]
        status = int(responses[-1]["status"]) if responses else 0
        blocked = re.search(r"access denied|verify you are human|checking your browser|just a moment|captcha",str(title)+text[:1000],re.I)
        has_visual = not text_has_promotion(text) and (bool(embedded) or bool(promotion_images(scans))
            or any(f.get("canvas") or f.get("generated_content") or f.get("obscured") for f in scans))
        accessible = 200<=status<400 and bool(text.strip() or has_visual or any(f.get("images") or f.get("backgrounds") for f in scans)) and not blocked
        if any(f.get("obscured") for f in scans): failures.append("image_obscured")
        tiles={"a":[],"b":[]}
        image_error=None
        if has_visual or force_ocr:
            try:
                for round_name in ("a","b"):
                    for index,(y,tile_height) in enumerate(tile_ranges(metrics["height"],shifted=round_name=="b")):
                        if time.monotonic()>=started+300:raise RuntimeError("image_stage_timeout")
                        page.evaluate(f"window.scrollTo(0,{y});new Promise(r=>requestAnimationFrame(()=>requestAnimationFrame(r)))")
                        clip={"x":0,"y":y,"width":width,"height":tile_height,"scale":2 if round_name=="b" else 1}
                        capture=page.call("Page.captureScreenshot",format="png",captureBeyondViewport=False,clip=clip)
                        data=base64.b64decode(capture["data"],validate=True)
                        path=directory/f"image_{round_name}_{index:03d}.png";atomic_bytes(path,data)
                        tile={"path":str(path),"clip":clip,"bytes":len(data)}
                        tile["png_digest"]=hashlib.sha256(data).hexdigest()
                        if round_name=="a":tile["pixel_digest"]=pixel_digest(data)
                        tiles[round_name].append(tile)
            except (RuntimeError,TimeoutError) as exc:
                failures.append("image_coverage_partial")
                image_error = str(exc)[:500]
        visual_digest=digest({"a":[t["pixel_digest"] for t in tiles["a"]],
                              "b":[t["png_digest"] for t in tiles["b"]]}) if tiles["a"] else None
        report = {"url":url,"final_url":final_url,"http_status":status,"title":title,"text":text,
            "frames":scans,"accessible":bool(accessible),"captured_at":now(),
            "image_capture_mode":"viewport_scrolled",
            "image_tiles":tiles,"visual_digest":visual_digest,"context_digest":digest(text),
            "image_seconds_remaining":max(0,300-(time.monotonic()-started)),"scroll_complete":bool(stable),
            "complete":bool(accessible and stable and not failures and resources_complete(scans,text)),
            "image_review_required":force_ocr or has_visual,
            "failures":failures,"image_error":image_error,"screenshot_path":str(directory / "screenshot.png")}
        report["product_links"] = page.evaluate(PRODUCT_LINKS)
        if "/collections/" in urlsplit(url).path and page.evaluate("""(()=>[...document.querySelectorAll('a[rel="next"],.pagination a[href*="page="],.pagination__item--next')].some(a=>a.getClientRects().length&&a.href&&a.href!==location.href))()"""):
            report["complete"] = False
            report["failures"].append("collection_pagination_unchecked")
        if collect_variants:
            variant_result = capture_product_variants(page, directory, width)
            report.update(variant_result)
            report["complete"] = report["complete"] and variant_result["variants_complete"]
        atomic_json(directory / "browser.json",report)
        return report
