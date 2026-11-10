from __future__ import annotations

import os
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest

import utils.monitor_health as monitor_health


def serve(routes):
    # 本机真实 HTTP 服务：按路径返回固定状态码
    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):
            status, body = routes.get(self.path, (404, b"{}"))
            self.send_response(status)
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, *args):
            pass

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    return server, f"http://127.0.0.1:{server.server_address[1]}"


def fake_ego(directory, body):
    # PATH 上的 ego-browser 可执行文件
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / "ego-browser"
    path.write_text("#!/bin/sh\n" + body + "\n")
    path.chmod(0o755)
    return str(path)


@pytest.fixture
def monitor_services(tmp_path_factory, monkeypatch):
    """监控运行前健康检查用到的三个服务全部正常：本机 Firecrawl、OCR 端点与 PATH 上应答的 ego-browser。"""
    firecrawl, firecrawl_url = serve({"/v0/health/liveness": (200, b'{"status":"ok"}')})
    ocr, ocr_url = serve({"/health": (200, b'{"ok": true}')})
    bin_dir = tmp_path_factory.mktemp("bin")
    fake_ego(bin_dir, 'echo "EGO_HEALTH function" >&2')
    monkeypatch.setattr(monitor_health, "FIRECRAWL_URL", firecrawl_url)
    monkeypatch.setattr(monitor_health, "OCR_URL", ocr_url)
    monkeypatch.setenv("PATH", f"{bin_dir}{os.pathsep}{os.environ['PATH']}")
    yield
    firecrawl.shutdown()
    ocr.shutdown()
