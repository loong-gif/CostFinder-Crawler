from __future__ import annotations

import hashlib
import io
import threading
from functools import partial
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

from PIL import Image, ImageDraw, ImageFont

from utils.paddle_ocr_markdown import ocr_lines
from utils.rendered_visibility_gate import filter_visible_membership_benefits
from utils.visibility_manifest import build_manifest, build_shard

PROJECT_ROOT = Path(__file__).resolve().parents[1]


def _image(rows: list[list[tuple[int, str]]]) -> Image.Image:
    """每行若干 (x 坐标, 文本)，用真实字体绘制。"""
    canvas = Image.new("RGB", (1400, 120 * len(rows) + 40), "white")
    draw = ImageDraw.Draw(canvas)
    font = ImageFont.load_default(size=48)
    for index, row in enumerate(rows):
        for x, text in row:
            draw.text((x, 40 + 120 * index), text, fill="black", font=font)
    return canvas


def _png(image: Image.Image) -> bytes:
    buffer = io.BytesIO()
    image.save(buffer, "PNG")
    return buffer.getvalue()


def test_image_ocr_has_a_single_entry_point():
    callers = sorted(str(path.relative_to(PROJECT_ROOT)) for folder in ("utils", "scripts")
                     for path in (PROJECT_ROOT / folder).rglob("*") if path.suffix in {".py", ".js", ".sh"}
                     and "tesseract" in path.read_text(errors="ignore"))
    assert callers == ["utils/paddle_ocr_markdown.py"], (
        f"{callers}: call utils.paddle_ocr_markdown.ocr_lines instead of running tesseract; the extraction "
        "pipeline and the visibility manifest must read images the same way")


def test_service_and_price_on_one_image_row_stay_on_one_ocr_line():
    lines = ocr_lines(_png(_image([[(40, "Botox"), (800, "$12/unit")]])))
    assert "Botox $12/unit" in lines


def test_repeated_tier_lines_are_all_kept():
    rows = [[(40, "Silver $99/month")], [(40, "1 facial per month")],
            [(40, "Gold $149/month")], [(40, "1 facial per month")]]
    assert ocr_lines(_png(_image(rows))).count("1 facial per month") == 2


def test_benefit_only_in_an_image_passes_the_membership_gate(tmp_path):
    _image([[(40, "VIP Membership")], [(40, "One facial per month $99")]]).save(tmp_path / "membership.png")
    server = ThreadingHTTPServer(("127.0.0.1", 0), partial(SimpleHTTPRequestHandler, directory=str(tmp_path)))
    threading.Thread(target=server.serve_forever, daemon=True).start()
    image_url = f"http://127.0.0.1:{server.server_address[1]}/membership.png"
    cached = PROJECT_ROOT / "state" / "visibility_ocr_images" / (hashlib.sha1(image_url.encode()).hexdigest()[:16] + ".img")
    page_url = "https://a.example/membership"
    plan = {"entries": [{"kind": "membership", "action": "insert", "source_url": page_url, "membership_name": "VIP Membership",
                         "benefits": ["One facial per month $99"]}]}
    render = {"results": [{"url": page_url, "checked_at": "2026-11-05T00:00:00Z",
                           "result": {"texts": ["VIP Membership", "Join today"], "images": [{"url": image_url}]}}]}
    try:
        manifest = build_manifest(build_shard(plan, "run"), render)
    finally:
        server.shutdown()
        cached.unlink(missing_ok=True)
    page = manifest[page_url]
    benefit = next(c for c in page["claimResults"] if c["claim"] == "One facial per month $99")
    assert benefit["status"] == "image_only"
    assert page["complete"] is True
    gate = filter_visible_membership_benefits(["One facial per month $99"], page)
    assert gate.accepted and gate.content == ("One facial per month $99",)
