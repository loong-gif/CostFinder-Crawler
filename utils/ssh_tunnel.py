"""监控服务器上只监听本机端口的服务（PaddleOCR、Google Maps scraper）：命令运行期间自动建立 SSH 本地端口转发。

服务已可达（隧道已存在或地址指向别处）时不建立隧道；否则启动 ``ssh -N -L`` 子进程，服务可达后执行命令，
命令结束时关闭子进程。隧道无法建立即终止命令。
"""
from __future__ import annotations

import subprocess
import time
from contextlib import contextmanager
from typing import Iterator, Sequence
from urllib.parse import urlparse

import requests

from utils.monitor_host import HOST as MONITOR_HOST

READY_TIMEOUT_S = 30.0


def service_up(probe_url: str) -> bool:
    try:
        requests.get(probe_url, timeout=5, proxies={"http": None, "https": None})
        return True
    except requests.ConnectionError:
        return False


def tunnel_command(service_url: str, remote_port: int, host: str = MONITOR_HOST) -> list[str]:
    port = urlparse(service_url).port
    return ["ssh", "-N", "-o", "ExitOnForwardFailure=yes", "-o", "BatchMode=yes",
            "-L", f"127.0.0.1:{port}:127.0.0.1:{remote_port}", host]


@contextmanager
def ssh_tunnel(service_url: str, remote_port: int, probe_path: str,
               command: Sequence[str] | None = None) -> Iterator[None]:
    probe = service_url.rstrip("/") + probe_path
    if service_up(probe):
        yield
        return
    process = subprocess.Popen(list(command or tunnel_command(service_url, remote_port)))
    try:
        deadline = time.monotonic() + READY_TIMEOUT_S
        while not service_up(probe):
            if process.poll() is not None or time.monotonic() > deadline:
                raise RuntimeError(f"ssh_tunnel_failed: {service_url} unreachable (exit {process.poll()})")
            time.sleep(0.5)
        yield
    finally:
        process.terminate()
        process.wait()
