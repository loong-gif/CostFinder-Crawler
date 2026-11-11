"""监控服务器（72.52.161.65）上 HTTP 服务的地址，只在这里定义。

本机直接调用公网地址（服务器防火墙只放行项目 IP）；代码运行在监控主机的部署目录时调用 127.0.0.1。
"""
from __future__ import annotations

from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
MONITOR_HOST_IP = "72.52.161.65"
MONITOR_DEPLOY_DIR = Path("/home/loong/costfinder_monitor")
PORTS = {"firecrawl": 3002, "gmaps": 8081, "ocr": 8082, "cpa": 8317}


def endpoints(project_root: Path) -> dict[str, str]:
    host = "127.0.0.1" if project_root == MONITOR_DEPLOY_DIR else MONITOR_HOST_IP
    return {name: f"http://{host}:{port}" for name, port in PORTS.items()}


_CURRENT = endpoints(PROJECT_ROOT)
FIRECRAWL_URL = _CURRENT["firecrawl"]
GMAPS_URL = _CURRENT["gmaps"]
OCR_URL = _CURRENT["ocr"]
CPA_URL = _CURRENT["cpa"]
