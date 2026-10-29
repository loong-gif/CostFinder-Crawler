"""Rendered evidence using the Windows Edge already installed on this server.

WSL 1 cannot run the container Chromium processes reliably. A dedicated
headless Edge profile exposes CDP on loopback; every temporary tab is closed.
"""
from __future__ import annotations

import base64
import json
import hashlib
import re
import subprocess
import time
from pathlib import Path
from itertools import product
from urllib.parse import urlsplit
from threading import BoundedSemaphore, Lock

import requests
from websockets.sync.client import connect

from utils.monitor_price_store import atomic_bytes, atomic_json, digest, now
from utils.monitor_image_evidence import tile_ranges, pixel_digest

DEBUG_URL = "http://127.0.0.1:9333"
ROOT = Path(__file__).resolve().parents[1]
BROWSER_SLOTS = BoundedSemaphore(2)
STARTUP_LOCK = Lock()
BREAKPOINTS = (("desktop", 1440, False), ("tablet", 768, False), ("mobile", 390, True))


FRAME_VISIBILITY = """function() {
  const r=this.getBoundingClientRect(), doc=this.ownerDocument.documentElement;
  let left=Math.max(0,r.left),right=Math.min(doc.clientWidth,r.right);
  let top=Math.max(-scrollY,r.top),bottom=Math.min(doc.scrollHeight-scrollY,r.bottom);
  for(let p=this;p;p=p.parentElement || p.getRootNode().host) {
    const s=getComputedStyle(p);
    if(s.display==='none' || s.visibility==='hidden' || Number(s.opacity)===0 || p.hidden) return {visible:false};
    if(p!==this && p!==doc && p!==this.ownerDocument.body) {
      const c=p.getBoundingClientRect();
      if(/hidden|clip|auto|scroll/.test(s.overflowX)) {left=Math.max(left,c.left+p.clientLeft);right=Math.min(right,c.left+p.clientLeft+p.clientWidth);}
      if(/hidden|clip|auto|scroll/.test(s.overflowY)) {top=Math.max(top,c.top+p.clientTop);bottom=Math.min(bottom,c.top+p.clientTop+p.clientHeight);}
    }
  }
  return {visible:right>left && bottom>top,bounds:{x:left+scrollX,y:top+scrollY,width:Math.max(0,right-left),height:Math.max(0,bottom-top)}};
}"""


def frame_regions(page, tree):
    regions = []
    for child in tree.get("childFrames", []):
        frame = child["frame"]
        try:
            owner = page.call("DOM.getFrameOwner", frameId=frame["id"])
            node = page.call("DOM.resolveNode", backendNodeId=owner["backendNodeId"])["object"]
            result = page.call("Runtime.callFunctionOn", objectId=node["objectId"],
                               functionDeclaration=FRAME_VISIBILITY, returnByValue=True)
            visible = result.get("result", {}).get("value", {})
            if visible.get("visible") is False:
                continue
        except (RuntimeError, KeyError):
            visible = {}
        # Child document text can lie outside its visible inner scroll window.
        regions.append({"kind":"embedded", "frame_id":frame["id"], "url":frame["url"],
                        "visibility_known":visible.get("visible") is True, **visible.get("bounds", {})})
    return regions


def resources_complete(frames: list[dict]) -> bool:
    return all(frame.get("ready") and not frame.get("unreviewed_regions")
               and all(image.get("loaded") is True for image in frame.get("images",[]))
               and all(image.get("decoded") is True and not image.get("network_failed")
               for image in frame.get("backgrounds",[])) for frame in frames)


def confirmed_not_found(browser: dict) -> bool:
    return bool(browser.get("http_status") == 404 and browser.get("scroll_complete")
                and Path(browser.get("screenshot_path", "")).is_file()
                and not browser.get("failures") and re.search(
                    r"\bpage\b.{0,30}\b(?:not\s+found|does not exist|doesn't exist)\b|"
                    r"\b404\s*[-: ]*\s*(?:page\s*)?(?:not\s+found|error)\b",
                    browser.get("text", ""), re.I))


def ensure_browser() -> None:
    with STARTUP_LOCK:
        _ensure_browser()


def _ensure_browser() -> None:
    session = requests.Session()
    session.trust_env = False
    try:
        session.get(DEBUG_URL + "/json/version",timeout=2).raise_for_status()
        return
    except requests.RequestException:
        pass
    candidates = [Path("/mnt/c/Program Files (x86)/Microsoft/Edge/Application/msedge.exe"),
                  Path("/mnt/c/Program Files/Microsoft/Edge/Application/msedge.exe")]
    edge = next((path for path in candidates if path.exists()),None)
    if edge is None:
        raise RuntimeError("rendered_browser_unavailable")
    profile = ROOT / "browser_profile"
    profile.mkdir(exist_ok=True)
    windows_profile = subprocess.check_output(["wslpath","-w",str(profile)],text=True).strip()
    subprocess.Popen([str(edge),"--headless=new","--remote-debugging-port=9333",
                      "--remote-debugging-address=127.0.0.1","--user-data-dir="+windows_profile,
                      "--no-first-run","--enable-automation","about:blank"],
                     stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL)
    for _ in range(30):
        try:
            session.get(DEBUG_URL + "/json/version",timeout=1).raise_for_status()
            return
        except requests.RequestException:
            time.sleep(.3)
    raise RuntimeError("rendered_browser_unavailable")


class BrowserPage:
    def __init__(self, socket, deadline=None):
        self.socket,self.sequence,self.events = socket,0,[]
        self.deadline = deadline or time.monotonic()+300

    def call(self, method, **params):
        self.sequence += 1
        self.socket.send(json.dumps({"id":self.sequence,"method":method,"params":params}))
        deadline = min(time.monotonic() + 65, self.deadline)
        while time.monotonic() < deadline:
            value = json.loads(self.socket.recv(timeout=max(.1,deadline-time.monotonic())))
            if value.get("id") != self.sequence:
                self.events.append(value)
                continue
            if value.get("error"):
                raise RuntimeError("rendered_browser_command_failed")
            return value.get("result",{})
        raise RuntimeError("rendered_page_timeout")

    def evaluate(self, expression, context_id=None):
        params = {"expression":expression,"awaitPromise":True,"returnByValue":True,"timeout":60000}
        if context_id is not None:
            params["contextId"] = context_id
        result = self.call("Runtime.evaluate",**params)
        if result.get("exceptionDetails"):
            raise RuntimeError("rendered_frame_failed")
        return result.get("result",{}).get("value")

    def wait_loaded(self):
        if any(event.get("method") == "Page.loadEventFired" for event in self.events):
            return
        deadline = min(time.monotonic() + 60, self.deadline)
        while time.monotonic() < deadline:
            event = json.loads(self.socket.recv(timeout=max(.1,deadline-time.monotonic())))
            self.events.append(event)
            if event.get("method") == "Page.loadEventFired":
                return
        raise RuntimeError("rendered_page_timeout")



PRODUCT_LINKS = """(()=>{
 const visible=el=>{for(let p=el;p;p=p.parentElement){const s=getComputedStyle(p);if(s.display==='none'||s.visibility==='hidden'||s.visibility==='collapse'||Number(s.opacity)===0||p.hidden)return false;}return [...el.getClientRects()].some(r=>r.width>0&&r.height>0);};
 return [...new Set([...document.querySelectorAll('main a[href*="/products/"], [role="main"] a[href*="/products/"]')]
  .filter(visible).map(a=>new URL(a.href,location.href)).filter(u=>u.origin===location.origin&&u.pathname.includes('/products/')).map(u=>u.origin+u.pathname))];
})()"""

VARIANT_CONTROLS = """(()=>{
 const visible=el=>{for(let p=el;p;p=p.parentElement){const s=getComputedStyle(p);if(s.display==='none'||s.visibility==='hidden'||s.visibility==='collapse'||Number(s.opacity)===0||p.hidden)return false;}return [...el.getClientRects()].some(r=>r.width>0&&r.height>0);};
 const root=document.querySelector('main')||document.body;
 const selects=[...root.querySelectorAll('variant-selects select, select[name^="options"], .product-form__input select')].filter(visible);
 const groups=selects.map((el,index)=>({kind:'select',index,values:[...el.options].filter(o=>!o.disabled&&o.value).map(o=>({value:o.value,label:o.textContent.trim()}))}));
 const names=[...new Set([...root.querySelectorAll('variant-radios input[type="radio"], variant-selects input[type="radio"], .product-form__input input[type="radio"]')].filter(el=>visible(el)||[...el.labels||[]].some(visible)).map(el=>el.name))];
 for(const name of names){const inputs=[...root.querySelectorAll('input[type="radio"]')].filter(el=>el.name===name&&!el.disabled&&[...el.labels||[]].some(visible));groups.push({kind:'radio',name,values:inputs.map(el=>({value:el.value,label:[...el.labels||[]].map(l=>l.textContent.trim()).join(' ').trim()||el.value}))});}
 return groups;
})()"""


def capture_product_variants(page, directory: Path, width: int) -> dict:
    """Operate only visible purchase controls; every selected price is rendered."""
    groups = page.evaluate(VARIANT_CONTROLS) or []
    if any(not group.get("values") for group in groups):
        return {"variant_captures": [], "variant_expected": 0, "variant_skipped": [],
                "variants_complete": False}
    combinations = list(product(*(group["values"] for group in groups))) if groups else [()]
    variant_expected = len(combinations)
    captures, variant_skipped, complete = [], [], True
    for index, selection in enumerate(combinations):
        labels = [value["label"] for value in selection]
        skip_base = {"index": index, "selected_options": labels}
        choices = [{**group, "selected": value} for group, value in zip(groups, selection)]
        changed = page.evaluate("""(async()=>{
          const root=document.querySelector('main')||document.body;
          const identity=()=>[...root.querySelectorAll('form[action*="/cart/add"] input[name="id"]')].map(e=>e.value).find(Boolean);
          const previous=identity();let changed=false;
          const visible=el=>{for(let p=el;p;p=p.parentElement){const s=getComputedStyle(p);if(s.display==='none'||s.visibility==='hidden'||s.visibility==='collapse'||Number(s.opacity)===0||p.hidden)return false;}return [...el.getClientRects()].some(r=>r.width>0&&r.height>0);};
          for(const g of """ + json.dumps(choices) + """) {
            const el=g.kind==='select'?[...root.querySelectorAll('variant-selects select, select[name^="options"], .product-form__input select')].filter(visible)[g.index]:[...root.querySelectorAll('input[type="radio"]')].find(x=>x.name===g.name&&x.value===g.selected.value&&(visible(x)||[...x.labels||[]].some(visible)));
            if(!el||el.disabled)return false;
            changed=changed||(g.kind==='select'?el.value!==g.selected.value:!el.checked);
            if(g.kind==='select'){const option=[...el.options].find(o=>o.value===g.selected.value);if(!option||option.disabled)return false;el.value=g.selected.value;el.dispatchEvent(new Event('change',{bubbles:true}));}else{el.click();}
            await new Promise(r=>setTimeout(r,500));
          }
          const end=Date.now()+15000;
          while(changed&&previous&&identity()===previous&&Date.now()<end){
            if([...root.querySelectorAll('form[action*="/cart/add"] button[type="submit"]')].every(b=>b.disabled))break;
            await new Promise(r=>setTimeout(r,250));
          }
          await new Promise(r=>setTimeout(r,600));
          return !changed||!previous||identity()!==previous||[...root.querySelectorAll('form[action*="/cart/add"] button[type="submit"]')].every(b=>b.disabled);
        })()""")
        if not changed:
            complete = False
            variant_skipped.append({**skip_base, "reason": "unchanged"})
            continue
        details = page.evaluate(r"""(()=>{
          const visible=el=>{for(let p=el;p;p=p.parentElement){const s=getComputedStyle(p);if(s.display==='none'||s.visibility==='hidden'||s.visibility==='collapse'||Number(s.opacity)===0||p.hidden)return false;}return [...el.getClientRects()].some(r=>r.width>0&&r.height>0);};
          const root=document.querySelector('main')||document.body;
          const forms=[...root.querySelectorAll('form[action*="/cart/add"]')];
          const buttons=forms.flatMap(f=>[...f.querySelectorAll('button[type="submit"],input[type="submit"]')]).filter(visible);
          if(!buttons.length)return {review:'purchase_button_missing'};
          if(buttons.every(b=>b.disabled||b.getAttribute('aria-disabled')==='true'))return {purchasable:false};
          const title=[...root.querySelectorAll('h1')].find(visible)?.textContent.trim();
          const prices=[...root.querySelectorAll('.product__info-container .price,[id^="price-"],.product-single__price,.product__price,.product-price')].filter(visible);
          const price=prices.find(el=>/\$\s*\d/.test(el.innerText));
          if(!title||!price)return {review:'visible_variant_price_missing'};
          const text=price.innerText.trim();
          const sale=[...price.querySelectorAll('.price-item--sale,.sale-price,[data-sale-price]')].find(visible);
          const regular=[...price.querySelectorAll('.price-item--regular,.regular-price,[data-regular-price]')].find(visible);
          const money=el=>{const hits=(el?.innerText||'').match(/\$\s*\d[\d,]*(?:\.\d+)?/g)||[];return hits.length===1?Number(hits[0].replace(/[^\d.]/g,'')):null;};
          const amounts=text.match(/\$\s*\d[\d,]*(?:\.\d+)?/g)||[];
          const discount=money(sale)??(amounts.length===1?Number(amounts[0].replace(/[^\d.]/g,'')):null);
          return {purchasable:true,title,price_text:text,discount_price:discount,regular_price:money(regular),
            variant_id:forms.map(f=>f.querySelector('[name="id"]')?.value).find(Boolean),url:location.href};
        })()""")
        if details.get("purchasable") is False:
            variant_skipped.append({**skip_base, "reason": "not_purchasable"})
            continue
        if details.get("review"):
            complete = False
            variant_skipped.append({**skip_base, "reason": details["review"]})
            continue
        if details.get("discount_price") is None:
            complete = False
            variant_skipped.append({**skip_base, "reason": "price_missing"})
            continue
        price_text = details.get("price_text", "")
        amounts = re.findall(r"\$\s*\d[\d,]*(?:\.\d+)?", price_text)
        from_label = bool(re.search(r"\bfrom\b", price_text, re.I))
        if from_label and len(amounts) != 1:
            complete = False
            variant_skipped.append({**skip_base, "reason": "from_price"})
            continue
        if from_label:
            details["starting_price_label"] = True
        quote = "\n".join([details["title"], *labels, details["price_text"]])
        capture = page.call("Page.captureScreenshot", format="png", captureBeyondViewport=True,
                            clip={"x":0,"y":0,"width":width,"height":min(page.call("Page.getLayoutMetrics")["cssContentSize"]["height"],32000),"scale":1})
        screenshot = directory / f"variant_{index}.png"
        atomic_bytes(screenshot, base64.b64decode(capture["data"], validate=True))
        second = page.call("Page.captureScreenshot", format="png", captureBeyondViewport=True,
                            clip={"x":0,"y":0,"width":width,"height":min(page.call("Page.getLayoutMetrics")["cssContentSize"]["height"],32000),"scale":2})
        screenshot_b = directory / f"variant_{index}_b.png"
        atomic_bytes(screenshot_b, base64.b64decode(second["data"], validate=True))
        captures.append({**details, "selected_options": labels, "offer_raw_text": quote,
                         "captured_at": now(), "screenshot_path": str(screenshot),
                         "second_screenshot_path": str(screenshot_b)})
    accounted = len(captures) + len(variant_skipped)
    if variant_expected and accounted != variant_expected:
        complete = False
    return {"variant_captures": captures, "variant_expected": variant_expected,
            "variant_skipped": variant_skipped, "variants_complete": complete and bool(captures or not variant_expected)}

def combine_breakpoint_reports(url: str, reports: list[dict]) -> dict:
    """Only a complete three-viewport union can establish absence."""
    good = [row for row in reports if row.get("accessible")]
    base = dict(good[0] if good else reports[0])
    coverage = {row["breakpoint"]: {"width": row["width"], "complete": row.get("complete") is True,
                 "accessible": row.get("accessible") is True, "captured_at": row.get("captured_at"),
                 "screenshot_path": row.get("screenshot_path"), "failures": row.get("failures", [])}
                for row in reports}
    complete = set(coverage) == {name for name, _, _ in BREAKPOINTS} and all(v["complete"] for v in coverage.values())
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


def _variant_capture_key(capture: dict, business_id: int | None) -> tuple:
    path = urlsplit(capture.get("url") or "").path
    variant_id = str(capture.get("variant_id") or "")
    if not variant_id:
        return (path, capture.get("offer_raw_text"))
    return (business_id, variant_id) if business_id is not None else (path, variant_id)


def _dedupe_variant_captures(captures: list[dict], business_id: int | None) -> list[dict]:
    seen, out = set(), []
    for capture in captures:
        key = _variant_capture_key(capture, business_id)
        if key in seen:
            continue
        seen.add(key)
        out.append(capture)
    return out


def _product_variant_reconciliation(detail: dict) -> dict:
    return {"url": detail.get("url"), "variant_expected": detail.get("variant_expected", 0),
            "variant_skipped": detail.get("variant_skipped") or [],
            "variant_capture_count": len(detail.get("variant_captures") or [])}


def render_page(url: str, directory: Path, *, force_ocr: bool = False, product_details: bool = True,
                business_id: int | None = None) -> dict:
    reports = []
    with BROWSER_SLOTS:
        for name, width, mobile in BREAKPOINTS:
            try:
                report = _render_page(url, directory / name, force_ocr=force_ocr,
                                      width=width, mobile=mobile, collect_variants="/products/" in urlsplit(url).path)
            except Exception as exc:
                report = {"url": url, "accessible": False, "complete": False, "failures": [type(exc).__name__]}
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
    ensure_browser()
    session = requests.Session(); session.trust_env = False
    target = session.put(DEBUG_URL + "/json/new?about:blank",timeout=5).json()
    try:
        with connect(target["webSocketDebuggerUrl"],open_timeout=5,max_size=40*1024*1024,proxy=None) as socket:
            page = BrowserPage(socket, started+300)
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
            has_visual = bool(embedded) or any(f.get("images") or f.get("backgrounds") or f.get("canvas") or f.get("generated_content") or f.get("obscured") for f in scans)
            accessible = 200<=status<400 and bool(text.strip() or has_visual) and not blocked
            if any(f.get("obscured") for f in scans): failures.append("image_obscured")
            tiles={"a":[],"b":[]}
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
                except (RuntimeError,TimeoutError):
                    failures.append("image_coverage_partial")
            visual_digest=digest({"a":[t["pixel_digest"] for t in tiles["a"]],
                                  "b":[t["png_digest"] for t in tiles["b"]]}) if tiles["a"] else None
            report = {"url":url,"final_url":final_url,"http_status":status,"title":title,"text":text,
                "frames":scans,"accessible":bool(accessible),"captured_at":now(),
                "image_capture_mode":"viewport_scrolled",
                "image_tiles":tiles,"visual_digest":visual_digest,"context_digest":digest(text),
                "image_seconds_remaining":max(0,300-(time.monotonic()-started)),"scroll_complete":bool(stable),
                "complete":bool(accessible and stable and not failures and resources_complete(scans)),
                "image_review_required":force_ocr or has_visual,
                "failures":failures,"screenshot_path":str(directory / "screenshot.png")}
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
    finally:
        session.get(DEBUG_URL + "/json/close/" + target["id"],timeout=5)
