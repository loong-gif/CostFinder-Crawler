"""让本机直接调用监控服务器上的服务：防火墙放行当前 IP，scraper 监听所有网卡，OCR 服务未运行时启动。

本机公网 IP 变化后（服务连接超时）重新运行一次。脚本内容见 utils/service_access.py。
"""
from __future__ import annotations

import argparse
import base64
import subprocess

from utils.monitor_host import HOST
from utils.service_access import server_script


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--host", default=HOST)
    args = parser.parse_args()
    encoded = base64.b64encode(server_script().encode("utf-16-le")).decode()
    result = subprocess.run(["ssh", "-o", "BatchMode=yes", args.host, "powershell", "-NoProfile", "-EncodedCommand",
                             encoded], capture_output=True, text=True, check=True, timeout=180)
    print(result.stdout.strip())
    return 0
