import json
import sys
from pathlib import Path

base = Path(sys.argv[1])
plans = [p for p in json.loads((base / "dry_run.json").read_text())["plans"] if p["action"] == "REVIEW_UNVERIFIED"]
pages = {}
for out in sorted((base / "render").glob("out*.json")):
    for r in json.loads(out.read_text())["results"]:
        pages[r["url"]] = r

for url in sorted({p["requested_url"] for p in plans}):
    r = pages[url]
    res = r.get("result") or {}
    texts = res.get("texts") or []
    print("=" * 100)
    print(url, "| final:", r.get("final_url"), "| title:", r.get("title"), "| error:", r.get("error"))
    print("controls", res.get("controls"), "clicked", res.get("clicked"), "images", len(res.get("images") or []))
    joined = "\n".join(t if isinstance(t, str) else json.dumps(t, ensure_ascii=False) for t in texts)
    print(joined[:int(sys.argv[2]) if len(sys.argv) > 2 else 3000])
    for p in plans:
        if p["requested_url"] == url:
            print("  PLAN", p["plan_id"], "biz", p["business_id"], p["membership_name_old"], p["price_old"], p["billing_old"], "deps", p["dependent_offer_count"])
