"""让本机直接调用监控服务器上的 Firecrawl、Google Maps scraper 与 OCR 服务。

在服务器上执行的 PowerShell：防火墙只放行本次 ssh 会话的来源 IP（与原有内网网段）访问三个服务端口，
scraper 改为监听所有网卡，OCR 服务未运行时启动。IP 变化后重新执行即可，规则按名称整条替换。
"""
from __future__ import annotations

from utils.service_endpoints import PORTS

RULE_NAME = "CostFinder services (owner IP)"
SCRAPER_CMD = r"C:\Users\loong\gmaps-scraper\run_scraper.cmd"


def server_script() -> str:
    ports = ",".join(str(port) for port in PORTS.values())
    return "\n".join([
        "$ErrorActionPreference = 'Stop'",
        "$ip = ($env:SSH_CLIENT -split ' ')[0]",
        f"$name = '{RULE_NAME}'",
        "if (Get-NetFirewallRule -DisplayName $name -ErrorAction Ignore) { Remove-NetFirewallRule -DisplayName $name }",
        f"New-NetFirewallRule -DisplayName $name -Direction Inbound -Action Allow -Protocol TCP -LocalPort {ports} "
        "-RemoteAddress '10.0.0.0/255.255.255.0',$ip | Out-Null",
        f"$cmd = '{SCRAPER_CMD}'",
        "$text = Get-Content $cmd -Raw",
        "if ($text -match '-addr 127\\.0\\.0\\.1:8081') {",
        "  Set-Content $cmd ($text -replace '-addr 127\\.0\\.0\\.1:8081', '-addr 0.0.0.0:8081') -NoNewline",
        "  schtasks /end /tn CostFinderGmapsScraper | Out-Null",
        "  Get-Process google_maps_scraper -ErrorAction Ignore | Stop-Process -Force",
        "  schtasks /run /tn CostFinderGmapsScraper | Out-Null",
        "}",
        "if (-not (Get-NetTCPConnection -LocalPort 8082 -State Listen -ErrorAction Ignore)) {",
        "  schtasks /run /tn CostFinderOcrService | Out-Null",
        "}",
        "\"allowed $ip on ports " + ports + "\"",
    ]) + "\n"
