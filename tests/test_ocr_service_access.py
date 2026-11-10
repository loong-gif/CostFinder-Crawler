from __future__ import annotations

import io
import json
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest
import requests
from PIL import Image

from utils import paddle_ocr_markdown
from utils.paddle_ocr_markdown import ocr_lines
from utils.render_review import ocr_image


class _Server:
    """本机 HTTP 服务：按路径返回预设响应，记录收到的请求体。"""

    def __init__(self, routes):
        self.routes, self.received = routes, []
        server = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *args):
                pass

            def _answer(self):
                body = self.rfile.read(int(self.headers.get("Content-Length") or 0))
                server.received.append((self.path, body))
                responses = server.routes[self.path]
                status, content_type, payload = responses.pop(0) if len(responses) > 1 else responses[0]
                self.send_response(status)
                self.send_header("Content-Type", content_type)
                self.send_header("Content-Length", str(len(payload)))
                self.end_headers()
                self.wfile.write(payload)

            do_GET = do_POST = _answer

        self.httpd = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.url = f"http://127.0.0.1:{self.httpd.server_address[1]}"
        threading.Thread(target=self.httpd.serve_forever, daemon=True).start()

    def close(self):
        self.httpd.shutdown()


def _image(fmt: str) -> bytes:
    buffer = io.BytesIO()
    Image.new("RGB", (300, 120), "white").save(buffer, fmt)
    return buffer.getvalue()


OK_LINES = (200, "application/json", json.dumps({"lines": ["Botox $10/unit"]}).encode())


def test_ocr_requests_are_retried_and_a_lasting_failure_stops_the_command(monkeypatch):
    service = _Server({"/ocr": [(503, "text/plain", b"busy"), (503, "text/plain", b"busy"), OK_LINES]})
    monkeypatch.setattr(paddle_ocr_markdown, "OCR_SERVICE_URL", service.url)
    try:
        assert ocr_lines(_image("PNG"), backoff=0) == ["Botox $10/unit"]
        assert len(service.received) == 3
        service.routes["/ocr"] = [(503, "text/plain", b"busy")]
        with pytest.raises(requests.HTTPError):
            ocr_lines(_image("PNG"), attempts=2, backoff=0)
    finally:
        service.close()


def test_images_visitors_cannot_load_or_that_are_not_raster_are_not_read_and_others_are(monkeypatch, tmp_path):
    ocr = _Server({"/ocr": [OK_LINES]})
    images = _Server({"/gone.png": [(404, "text/html", b"not found")],
                      "/logo.svg": [(200, "image/svg+xml", b"<svg xmlns='http://www.w3.org/2000/svg'/>")],
                      "/promo.gif": [(200, "image/gif", _image("GIF"))]})
    monkeypatch.setattr(paddle_ocr_markdown, "OCR_SERVICE_URL", ocr.url)
    try:
        assert ocr_image(f"{images.url}/gone.png", tmp_path) == {"url": f"{images.url}/gone.png",
                                                                 "unavailable": "http_404"}
        assert ocr_image(f"{images.url}/logo.svg", tmp_path) == {"url": f"{images.url}/logo.svg",
                                                                 "unavailable": "not_raster_image:image/svg+xml"}
        assert ocr_image(f"{images.url}/promo.gif", tmp_path) == {"url": f"{images.url}/promo.gif",
                                                                  "text": "Botox $10/unit"}
        # GIF 转成 PNG 再交给 OCR 服务（服务只解码常见位图格式）
        assert Image.open(io.BytesIO(ocr.received[-1][1])).format == "PNG"
    finally:
        ocr.close()
        images.close()
