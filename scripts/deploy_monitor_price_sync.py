#!/usr/bin/env python3
"""Deploy the existing monitor task.

File payloads (including explicitly selected credentials) travel over SSH stdin,
never command arguments or output. This tool performs no database operations; the
deployed jobs never write the database, they only produce SQL files under
``$BASE/state/pending_sql/`` that ``pending-sql --pull`` brings back and the session runs through the Supabase MCP.
"""
from __future__ import annotations

import argparse
import base64
import io
import json
import subprocess
import sys
import tarfile
from pathlib import Path

from dotenv import dotenv_values

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from utils.monitor_host import BASE, remote_python  # noqa: E402


def monitor_launcher(log: str, run_type: str, extra_args: str, *, yield_to_running: bool = False) -> bytes:
    """Run the monitor, then send the Feishu run summary; the task keeps the monitor's exit code.

    yield_to_running: 完整 monitor 持有锁时本次直接结束（重试任务的工作由正在运行的完整 monitor 覆盖）。
    """
    yield_check = ('flock -n "$BASE/state/.monitor.lock" true || { echo "monitor running, skipped"; exit 0; }\n'
                   if yield_to_running else '')
    return (
        '#!/bin/sh\nset -eu\nBASE="'+BASE+'"\nmkdir -p "$BASE/logs" "$BASE/state"\n'
        'exec >>"$BASE/logs/'+log+'" 2>&1\ndate -u\n' + yield_check +
        'export FIRECRAWL_SELF_HOSTED_API_URL="http://127.0.0.1:3002"\nexport PYTHONUNBUFFERED=1\n'
        'PY="$BASE/.venv/bin/python"\nSTARTED="$(date -u +%Y-%m-%dT%H:%M:%S+00:00)"\nCODE=0\n'
        '"$PY" "$BASE/scripts/promo_pipeline.py" monitor --state-dir "$BASE/state"'+extra_args+' || CODE=$?\n'
        '"$PY" "$BASE/scripts/promo_pipeline.py" notify-run --state-dir "$BASE/state" '
        '--run-type '+run_type+' '
        '--started-at "$STARTED" --exit-code "$CODE" --log "$BASE/logs/'+log+'"\n'
        'exit "$CODE"\n').encode()


def deploy(host: str, include_credentials: bool):
    files = (list((ROOT / "utils").glob("*.py")) + list((ROOT / "schema").glob("*.json"))
             + list((ROOT / "config").glob("*.py")))
    files += [ROOT / relative for relative in (
        "scripts/firecrawl_self_hosted_monitor.py","scripts/run_monitor_price_sync.py","scripts/monitor_schedule.py",
        "scripts/notify_monitor_run.py",
        "scripts/run_data_quality_audit.py",
        "scripts/crawl_scheduler.py","scripts/seed_crawl_schedule.py",
        "scripts/process_discovery_results.py","scripts/select_next_city.py","scripts/crawl_report.py",
        "scripts/crawl_record_from_monitor.py","scripts/gmaps_discovery.py","scripts/promo_pipeline.py",
        "scripts/discover_new_promo_pages.py","scripts/deactivate_expired.py","scripts/pending_sql.py",
        "scripts/monitor_rendered_page.js","scripts/monitor_ego_bridge.js","requirements_monitor_price_sync.txt",
        "service_name_dict.json")]
    payload = {str(path.relative_to(ROOT)):path.read_bytes() for path in files}
    if include_credentials:
        values = dotenv_values(ROOT / ".env")
        keys = ("SUPABASE_URL","SUPABASE_SECRET_KEY","SUPABASE_SERVICE_ROLE_KEY",
                "FIRECRAWL_SELF_HOSTED_API_URL","FIRECRAWL_SELF_HOSTED_API_KEY",
                "LLM_API_KEY","LLM_API_URL","LLM_API_BASE","LLM_MODEL","LLM_BACKEND","GEMINI_API_KEY",
                "FEISHU_APP_ID","FEISHU_APP_SECRET","FEISHU_NOTIFY_OPEN_ID")
        payload[".env"] = ("\n".join(key+"="+json.dumps(values[key]) for key in keys if values.get(key))+"\n").encode()
    payload["run_crawl_due.sh"] = (ROOT / "scripts/run_crawl_due.sh").read_bytes()
    payload["run_monitor_validation.sh"] = monitor_launcher("monitor.log", "full_monitor", "")
    payload["run_monitor_retry.sh"] = monitor_launcher("retry.log", "failure_retry", " --retry-transient-only",
                                                       yield_to_running=True)
    # Triggered hourly in server (Pacific) time; only the 09:00 Shanghai hour runs, so DST cannot shift it.
    payload["run_data_quality_daily.sh"] = (
        '#!/bin/sh\nset -eu\n[ "$(TZ=Asia/Shanghai date +%H)" = "09" ] || exit 0\nBASE="'+BASE+'"\n'
        'mkdir -p "$BASE/logs" "$BASE/state"\nexec >>"$BASE/logs/data_quality.log" 2>&1\ndate -u\n'
        'export FIRECRAWL_SELF_HOSTED_API_URL="http://127.0.0.1:3002"\nexport PYTHONUNBUFFERED=1\n'
        '"$BASE/.venv/bin/python" "$BASE/scripts/promo_pipeline.py" deactivate-expired --out-dir "$BASE/state/pending_sql"\n'
        'exec "$BASE/.venv/bin/python" "$BASE/scripts/promo_pipeline.py" quality '
        '--state-dir "$BASE/state" --recheck\n').encode()
    # PaddleOCR 服务：独立的 Python 3.12 环境（/home/loong/ocr_service），只监听本机 127.0.0.1:8082。
    payload["run_ocr_service.sh"] = (
        '#!/bin/sh\nset -eu\nBASE="'+BASE+'"\nOCR=/home/loong/ocr_service\nexec >>"$BASE/logs/ocr_service.log" 2>&1\ndate -u\n'
        'export LD_LIBRARY_PATH="$OCR/lib_root/usr/lib/x86_64-linux-gnu"\nexport PYTHONUNBUFFERED=1\n'
        'cd "$BASE"\nexec "$OCR/venv/bin/python" -m utils.ocr_service\n').encode()
    stream = io.BytesIO()
    with tarfile.open(fileobj=stream,mode="w:gz") as archive:
        for name,content in payload.items():
            info=tarfile.TarInfo(name); info.size=len(content); info.mode=0o600 if name == ".env" else 0o644
            archive.addfile(info,io.BytesIO(content))
    code = '''import base64,io,tarfile,sys,json,os,shutil,fcntl
from pathlib import Path
from datetime import datetime,timezone
base=Path("/home/loong/costfinder_monitor")
state=base/"state"; state.mkdir(parents=True,exist_ok=True)
(state/"pending_sql").mkdir(exist_ok=True)
lock=open(state/".monitor.lock","a+")
fcntl.flock(lock,fcntl.LOCK_EX|fcntl.LOCK_NB)
backup=base/"price_sync_backups"/datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S")
backup.mkdir(parents=True,exist_ok=True)
archive=tarfile.open(fileobj=io.BytesIO(base64.b64decode(sys.stdin.buffer.read())),mode="r:gz")
count=0
for member in archive.getmembers():
    relative=Path(member.name)
    if relative.is_absolute() or ".." in relative.parts or not member.isfile(): raise ValueError("invalid_archive")
    target=base/relative
    if target.exists():
        prior=backup/relative; prior.parent.mkdir(parents=True,exist_ok=True); shutil.copy2(target,prior)
    target.parent.mkdir(parents=True,exist_ok=True)
    target.write_bytes(archive.extractfile(member).read()); target.chmod(member.mode); count+=1
(base/"run_monitor_validation.sh").chmod(0o755)
(base/"run_monitor_retry.sh").chmod(0o755)
(base/"run_data_quality_daily.sh").chmod(0o755)
(base/"run_crawl_due.sh").chmod(0o755)
(base/"run_ocr_service.sh").chmod(0o755)
print(json.dumps({"files_deployed":count,"backup":str(backup)}))
'''
    print(remote_python(code,stdin=base64.b64encode(stream.getvalue()),host=host))


def remote_powershell(script: str, *, timeout: int = 120, host: str = "firecrawl") -> str:
    encoded = base64.b64encode(script.encode("utf-16-le")).decode()
    result = subprocess.run(["ssh","-o","BatchMode=yes","-o","ConnectTimeout=10",host,
                             "powershell -NoProfile -NonInteractive -EncodedCommand " + encoded],
                            capture_output=True,timeout=timeout)
    if result.returncode:
        raise RuntimeError("remote_operation_failed:" + result.stderr.decode(errors="replace")[-300:])
    return result.stdout.decode(errors="replace")


CONSOLE_SESSION_TASK = "CostFinderKeepConsoleSession"
# 断开远程桌面后 Windows 不再绘制该会话的桌面：ego-lite 不出帧，同一页面只能截第一张图，requestAnimationFrame 不触发。
# loong 的会话一断开（TerminalServices-LocalSessionManager 事件 24）就由 SYSTEM 用 tscon 转到控制台，会话保持登录且持续绘制。
KEEP_CONSOLE_COMMAND = ('$line = query session loong | Select-Object -Skip 1 -First 1; '
                        'if ($line -match "\\s(\\d+)\\s+Disc") { tscon $Matches[1] /dest:console; exit $LASTEXITCODE }; '
                        'exit 0')


def ensure_console_session_task(host: str) -> str:
    command = base64.b64encode(KEEP_CONSOLE_COMMAND.encode("utf-16-le")).decode()
    script = (
        "$ProgressPreference = 'SilentlyContinue'\n"
        "$class = Get-CimClass -ClassName MSFT_TaskEventTrigger -Namespace Root/Microsoft/Windows/TaskScheduler\n"
        "$trigger = New-CimInstance -CimClass $class -ClientOnly\n"
        "$trigger.Enabled = $true\n"
        "$trigger.Subscription = '<QueryList><Query Id=\"0\" Path=\"Microsoft-Windows-TerminalServices-LocalSessionManager/Operational\">"
        "<Select Path=\"Microsoft-Windows-TerminalServices-LocalSessionManager/Operational\">*[System[EventID=24]]</Select>"
        "</Query></QueryList>'\n"
        f"$action = New-ScheduledTaskAction -Execute 'powershell.exe' -Argument '-NoProfile -NonInteractive -EncodedCommand {command}'\n"
        "$principal = New-ScheduledTaskPrincipal -UserId 'SYSTEM' -LogonType ServiceAccount -RunLevel Highest\n"
        f"Register-ScheduledTask -TaskName '{CONSOLE_SESSION_TASK}' -Trigger $trigger -Action $action -Principal $principal -Force | Out-Null\n"
        f"(Get-ScheduledTask -TaskName '{CONSOLE_SESSION_TASK}').State\n")
    return remote_powershell(script, host=host)


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--host",default="firecrawl")
    parser.add_argument("--include-credentials",action="store_true")
    args=parser.parse_args()
    deploy(args.host,args.include_credentials)
    print(json.dumps({"console_session_task": ensure_console_session_task(args.host).strip().splitlines()[-1]}))


if __name__ == "__main__":
    main()
