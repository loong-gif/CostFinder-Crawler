# 监控运行前的健康检查：ego-lite、OCR 服务与 Firecrawl。任一失败时本轮的失败记为基础设施故障，不计入来源的天数。
from __future__ import annotations

import subprocess

import requests

from utils.ego_browser import ego_command as default_ego_command
from utils.service_endpoints import FIRECRAWL_URL, OCR_URL

EGO_HEALTH_SCRIPT = "const task = await taskSpace('costfinder health'); console.log('EGO_HEALTH ' + typeof task.page);"
EGO_HEALTH_TIMEOUT_S = 90
HTTP_TIMEOUT_S = 10


def http_failure(url: str) -> str | None:
    session = requests.Session()
    session.trust_env = False
    # 健康检查的结论就是服务是否应答：连接失败记为该服务的故障
    try:
        response = session.get(url, timeout=HTTP_TIMEOUT_S)
    except requests.RequestException as exc:
        return type(exc).__name__
    return None if response.ok else f"HTTP {response.status_code}"


def ego_failure(command: str) -> str | None:
    # ego-lite 能打开任务空间才算可用；console 输出写到 stderr
    try:
        done = subprocess.run([command, "nodejs", "-e", EGO_HEALTH_SCRIPT], capture_output=True, text=True,
                              encoding="utf-8", timeout=EGO_HEALTH_TIMEOUT_S)
    except subprocess.TimeoutExpired:
        return "timeout"
    output = done.stdout + done.stderr
    if done.returncode == 0 and "EGO_HEALTH function" in output:
        return None
    return f"exit {done.returncode}: {output.strip()[-300:]}"


def service_failures(*, firecrawl_url: str | None = None, ocr_url: str | None = None,
                     ego_command: str | None = None) -> list[dict]:
    """未指定的地址与命令取 utils.service_endpoints 的服务地址和 PATH 上的 ego-browser。"""
    checks = [("firecrawl", http_failure(f"{firecrawl_url or FIRECRAWL_URL}/v0/health/liveness")),
              ("ocr", http_failure(f"{ocr_url or OCR_URL}/health")),
              ("ego-lite", ego_failure(ego_command or default_ego_command()))]
    return [{"service": service, "error": error} for service, error in checks if error is not None]
