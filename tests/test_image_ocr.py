from __future__ import annotations

import hashlib
import io
import re
import threading
from functools import partial
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import pytest
import requests
from PIL import Image, ImageDraw, ImageFont

from utils.ocr_rows import rows_from_detections
from utils.paddle_ocr_markdown import OCR_SERVICE_URL, ocr_lines
from utils.rendered_visibility_gate import filter_visible_membership_benefits
from utils.visibility_manifest import build_manifest, build_shard

# 正常渲染页面的导航与正文，长度足以判断页面已渲染
PAGE_CHROME = ["Home", "About Us", "Services", "Book Now", "Contact",
               "Our board-certified injectors have served the community for over ten years with natural results.",
               "Call us today to schedule a complimentary consultation with one of our aesthetic specialists.",
               "Hours: Monday to Friday 9am - 6pm, Saturday 10am - 4pm. Located downtown near the main square."]

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


def _ocr_service_reachable() -> bool:
    try:
        return requests.get(f"{OCR_SERVICE_URL}/health", timeout=5, proxies={"http": None, "https": None}).ok
    except requests.RequestException:
        return False


requires_ocr_service = pytest.mark.skipif(
    not _ocr_service_reachable(),
    reason="PaddleOCR service unreachable: ssh -f -N -L 127.0.0.1:18082:127.0.0.1:8082 firecrawl")


def test_image_ocr_has_a_single_entry_point():
    engines = sorted(str(path.relative_to(PROJECT_ROOT)) for folder in ("utils", "scripts")
                     for path in (PROJECT_ROOT / folder).rglob("*") if path.suffix in {".py", ".js", ".sh"}
                     and re.search(r"^\s*(from|import)\s+paddleocr|tesseract", path.read_text(errors="ignore"), re.M))
    assert engines == ["utils/ocr_service.py"], (
        f"{engines}: only utils/ocr_service.py runs the OCR engine; call utils.paddle_ocr_markdown.ocr_lines so the "
        "extraction pipeline and the visibility manifest read images the same way")


def test_boxes_on_one_row_merge_into_one_line_and_rows_read_top_to_bottom():
    detections = [([[800, 60], [1000, 60], [1000, 110], [800, 110]], "$12/unit"),
                  ([[40, 200], [400, 200], [400, 250], [40, 250]], "1 facial per month"),
                  ([[40, 58], [200, 58], [200, 112], [40, 112]], "Botox"),
                  ([[40, 330], [400, 330], [400, 380], [40, 380]], "1 facial per month"),
                  ([[40, 400], [100, 400], [100, 440], [40, 440]], "  ")]
    assert rows_from_detections(detections) == ["Botox $12/unit", "1 facial per month", "1 facial per month"]


@requires_ocr_service
def test_service_and_price_on_one_image_row_stay_on_one_ocr_line():
    lines = ocr_lines(_png(_image([[(40, "Botox"), (800, "$12/unit")]])))
    assert "Botox $12/unit" in lines


@requires_ocr_service
def test_repeated_tier_lines_are_all_kept():
    rows = [[(40, "Silver $99/month")], [(40, "1 facial per month")],
            [(40, "Gold $149/month")], [(40, "1 facial per month")]]
    assert ocr_lines(_png(_image(rows))).count("1 facial per month") == 2


@requires_ocr_service
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
                           "result": {"texts": [*PAGE_CHROME, "VIP Membership", "Join today"],
                                      "images": [{"url": image_url}]}}]}
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
