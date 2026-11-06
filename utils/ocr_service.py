"""PaddleOCR 服务，运行在监控服务器的 OCR 专用环境里，只监听本机 127.0.0.1。

POST /ocr 收一张图片的原始字节，返回 {"lines": [...]}（行合并见 utils/ocr_rows.py）；GET /health 返回 {"ok": true}。
本机流水线通过 SSH 隧道调用，入口见 utils/paddle_ocr_markdown.py 的 ocr_lines。
"""
from __future__ import annotations

import json
import os
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from threading import Lock

import cv2
import numpy as np
from paddleocr import PaddleOCR

from utils.ocr_rows import rows_from_detections

HOST = "127.0.0.1"
MAX_IMAGE_BYTES = 30 * 1024 * 1024

# oneDNN 加速在 PaddlePaddle 3.x 的静态图推理里会报 ConvertPirAttribute2RuntimeAttribute，关闭它
ENGINE = PaddleOCR(lang="en", use_doc_orientation_classify=False, use_doc_unwarping=False,
                   use_textline_orientation=False, enable_mkldnn=False)
INFERENCE_LOCK = Lock()


def recognize(image_bytes: bytes) -> list[str]:
    image = cv2.imdecode(np.frombuffer(image_bytes, dtype=np.uint8), cv2.IMREAD_COLOR)
    if image is None:
        raise ValueError("undecodable_image")
    with INFERENCE_LOCK:
        page = ENGINE.predict(image)[0]
    return rows_from_detections(zip((polygon.tolist() for polygon in page["rec_polys"]), page["rec_texts"]))


class Handler(BaseHTTPRequestHandler):
    def _reply(self, status: int, payload: dict) -> None:
        body = json.dumps(payload, ensure_ascii=False).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self) -> None:
        self._reply(200, {"ok": True}) if self.path == "/health" else self._reply(404, {"error": "not_found"})

    def do_POST(self) -> None:
        if self.path != "/ocr":
            return self._reply(404, {"error": "not_found"})
        length = int(self.headers.get("Content-Length") or 0)
        if not 0 < length <= MAX_IMAGE_BYTES:
            return self._reply(413, {"error": "image_size_out_of_range"})
        self._reply(200, {"lines": recognize(self.rfile.read(length))})


def main() -> None:
    port = int(os.environ.get("OCR_SERVICE_PORT", "8082"))
    ThreadingHTTPServer((HOST, port), Handler).serve_forever()


if __name__ == "__main__":
    main()
