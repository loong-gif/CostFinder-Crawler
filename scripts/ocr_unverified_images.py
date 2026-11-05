import json
import sys
from pathlib import Path

import requests

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from utils.paddle_ocr_markdown import ocr_lines  # noqa: E402

base = Path(sys.argv[1])
needles = sys.argv[2:]
for out in sorted((base / "render").glob("out*.json")):
    for r in json.loads(out.read_text())["results"]:
        if not any(n in r["url"] for n in needles):
            continue
        res = r.get("result") or {}
        print("=" * 80, r["url"])
        for line in "\n".join(res.get("texts") or []).splitlines():
            if "$" in line or "/mo" in line.lower() or "month" in line.lower():
                print("TEXT:", line)
        for img in res.get("images") or []:
            if img["w"] < 200 or img["h"] < 80:
                continue
            lines = ocr_lines(requests.get(img["url"], timeout=60).content)
            print("IMG", img["url"], img["w"], img["h"])
            print(" ".join(lines)[:1500])
