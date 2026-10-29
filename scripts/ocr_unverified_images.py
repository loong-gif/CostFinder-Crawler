import json
import subprocess
import sys
import tempfile
from pathlib import Path

import requests

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
            data = requests.get(img["url"], timeout=60).content
            with tempfile.NamedTemporaryFile(suffix=Path(img["url"]).suffix or ".jpg") as f:
                f.write(data)
                f.flush()
                done = subprocess.run(["tesseract", f.name, "stdout", "--psm", "11"], capture_output=True, text=True)
            print("IMG", img["url"], img["w"], img["h"], "tesseract_exit", done.returncode)
            print(" ".join(done.stdout.split())[:1500])
