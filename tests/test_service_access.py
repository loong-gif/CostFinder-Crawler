import base64
import subprocess
import sys

from scripts import service_access
from utils.service_access import RULE_NAME, server_script
from utils.service_endpoints import PORTS


def test_script_allows_only_the_caller_ip_on_the_service_ports():
    script = server_script()
    ports = ",".join(str(p) for p in PORTS.values())
    assert f"-LocalPort {ports}" in script and RULE_NAME in script
    # 远程地址只有 ssh 会话的来源 IP 与原有的内网网段
    assert "-RemoteAddress '10.0.0.0/255.255.255.0',$ip" in script
    assert "($env:SSH_CLIENT -split ' ')[0]" in script


def test_script_binds_the_scraper_to_all_interfaces_and_starts_the_ocr_service():
    script = server_script()
    assert "-addr 0.0.0.0:8081" in script and "CostFinderGmapsScraper" in script
    assert "schtasks /run /tn CostFinderOcrService" in script


def test_command_sends_the_script_to_the_monitor_host_as_an_encoded_powershell_command(monkeypatch, capsys):
    calls = []

    def run(cmd, capture_output, text, check, timeout):
        calls.append(cmd)
        return subprocess.CompletedProcess(cmd, 0, "allowed 58.44.8.32\n", "")

    monkeypatch.setattr(service_access.subprocess, "run", run)
    monkeypatch.setattr(sys, "argv", ["service-access"])
    assert service_access.main() == 0
    (cmd,) = calls
    assert cmd[:4] == ["ssh", "-o", "BatchMode=yes", "firecrawl"]
    assert cmd[4:7] == ["powershell", "-NoProfile", "-EncodedCommand"]
    assert base64.b64decode(cmd[7]).decode("utf-16-le") == server_script()
    assert "allowed 58.44.8.32" in capsys.readouterr().out
