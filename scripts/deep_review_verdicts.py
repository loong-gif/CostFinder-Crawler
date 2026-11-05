import argparse
import glob
import json
import subprocess
import sys
from pathlib import Path

import requests

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from utils.render_review import deep_verdict, ocr_image  # noqa: E402


def main() -> int:
    parser = argparse.ArgumentParser(description="Verdicts for the deep render review: expanded text plus image OCR")
    parser.add_argument("--dir", default="state/discovery/render_review/deep")
    parser.add_argument("--out", default="state/discovery/render_review/deep/verdicts.json")
    args = parser.parse_args()

    directory = Path(args.dir)
    image_dir = directory / "images"
    image_dir.mkdir(exist_ok=True)
    claims_by_url = {}
    for path in glob.glob(str(directory / "shard*.json")):
        for item in json.loads(Path(path).read_text())["items"]:
            claims_by_url[item["url"]] = item["claims"]

    records = {}
    for path in sorted(glob.glob(str(directory / "out*.json"))):
        for record in json.loads(Path(path).read_text())["results"]:
            records[record["url"]] = record

    verdicts = {}
    for url, claims in claims_by_url.items():
        record = records.get(url)
        if record is None or record.get("error"):
            verdicts[url] = {"verdict": "page_unavailable", "reason": (record or {}).get("error", "not_run")}
            continue
        result = record["result"]
        texts = result["texts"]
        verdict = deep_verdict(claims, texts)
        if verdict["verdict"] != "verified":
            image_texts = []
            for image in result["images"]:
                # 单张图片无法下载或识别时，该图片不提供可见性证据
                try:
                    image_texts.append(ocr_image(image["url"], image_dir))
                except (requests.RequestException, subprocess.CalledProcessError, FileNotFoundError) as exc:
                    print(f"image OCR failed: {image['url']}: {exc}")
                    continue
            verdict = deep_verdict(claims, texts, image_texts)
            verdict["images_read"] = len(image_texts)
        verdict.update({"controls": result["controls"], "clicked": result["clicked"], "visible_text_nodes": len(texts),
                        "images": len(result["images"]), "checked_at": record["checked_at"], "final_url": record.get("final_url")})
        verdicts[url] = verdict

    Path(args.out).write_text(json.dumps(verdicts, ensure_ascii=False, indent=1))
    for url, v in verdicts.items():
        print(v["verdict"], v.get("source", ""), v["reason"][:70], url)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
