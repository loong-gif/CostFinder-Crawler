from __future__ import annotations

import base64
import json
import re
from itertools import product
from pathlib import Path
from urllib.parse import urlsplit

from utils.monitor_price_store import atomic_bytes, now

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
