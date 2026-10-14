#!/usr/bin/env python3
"""Deploy the existing monitor task with price writes disabled by default.

File payloads (including explicitly selected credentials) travel over SSH stdin,
never command arguments or output. This tool performs no database operations.
"""
from __future__ import annotations

import argparse
import base64
import io
import json
import subprocess
import tarfile
from pathlib import Path

from dotenv import dotenv_values

ROOT = Path(__file__).resolve().parents[1]
BASE = "/home/loong/costfinder_monitor"


def remote_python(code: str, *, stdin: bytes = b"", timeout: int = 60, host: str = "firecrawl"):
    encoded = base64.b64encode(code.encode()).decode()
    command = 'wsl -e python3 -c "import base64;exec(base64.b64decode(\'' + encoded + '\'))"'
    result = subprocess.run(["ssh","-o","BatchMode=yes","-o","ConnectTimeout=10",host,command],
                            input=stdin,capture_output=True,timeout=timeout)
    if result.returncode:
        raise RuntimeError("remote_operation_failed:" + result.stderr.decode(errors="replace")[-300:])
    return result.stdout.decode()


def deploy(host: str, include_credentials: bool):
    files = (list((ROOT / "utils").glob("*.py")) + list((ROOT / "schema").glob("*.json"))
             + list((ROOT / "config").glob("*.py")))
    files += [ROOT / relative for relative in (
        "scripts/firecrawl_self_hosted_monitor.py","scripts/run_monitor_price_sync.py",
        "scripts/run_data_quality_audit.py",
        "scripts/monitor_rendered_page.js","requirements_monitor_price_sync.txt",
        "service_name_dict.json","config/firecrawl_monitor_migration.json")]
    payload = {str(path.relative_to(ROOT)):path.read_bytes() for path in files}
    if include_credentials:
        values = dotenv_values(ROOT / ".env")
        keys = ("SUPABASE_URL","SUPABASE_SECRET_KEY","SUPABASE_SERVICE_ROLE_KEY",
                "FIRECRAWL_SELF_HOSTED_API_URL","FIRECRAWL_SELF_HOSTED_API_KEY",
                "LLM_API_KEY","LLM_API_URL","LLM_API_BASE","LLM_MODEL","LLM_BACKEND","GEMINI_API_KEY","DB_URL")
        payload[".env"] = ("\n".join(key+"="+json.dumps(values[key]) for key in keys if values.get(key))+"\n").encode()
    payload["run_monitor_validation.sh"] = (
        '#!/bin/sh\nset -eu\nBASE="'+BASE+'"\nmkdir -p "$BASE/logs" "$BASE/state"\n'
        'exec >>"$BASE/logs/monitor.log" 2>&1\ndate -u\n'
        'export FIRECRAWL_SELF_HOSTED_API_URL="http://127.0.0.1:3002"\nexport PYTHONUNBUFFERED=1\nexec "$BASE/.venv/bin/python" "$BASE/scripts/run_monitor_price_sync.py" '
        '--state-dir "$BASE/state" --config "$BASE/config/firecrawl_monitor_migration.json"\n').encode()
    payload["run_monitor_retry.sh"] = (
        '#!/bin/sh\nset -eu\nBASE="'+BASE+'"\nmkdir -p "$BASE/logs" "$BASE/state"\n'
        'exec >>"$BASE/logs/retry.log" 2>&1\ndate -u\n'
        'export FIRECRAWL_SELF_HOSTED_API_URL="http://127.0.0.1:3002"\nexport PYTHONUNBUFFERED=1\nexec "$BASE/.venv/bin/python" "$BASE/scripts/run_monitor_price_sync.py" '
        '--state-dir "$BASE/state" --config "$BASE/config/firecrawl_monitor_migration.json" --retry-transient-only\n').encode()
    # Triggered hourly in server (Pacific) time; only the 09:00 Shanghai hour runs, so DST cannot shift it.
    payload["run_data_quality_daily.sh"] = (
        '#!/bin/sh\nset -eu\n[ "$(TZ=Asia/Shanghai date +%H)" = "09" ] || exit 0\nBASE="'+BASE+'"\n'
        'mkdir -p "$BASE/logs" "$BASE/state"\nexec >>"$BASE/logs/data_quality.log" 2>&1\ndate -u\n'
        'export FIRECRAWL_SELF_HOSTED_API_URL="http://127.0.0.1:3002"\nexport PYTHONUNBUFFERED=1\nexec "$BASE/.venv/bin/python" "$BASE/scripts/run_data_quality_audit.py" '
        '--state-dir "$BASE/state" --recheck\n').encode()
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
lock=open(state/".monitor.lock","a+")
fcntl.flock(lock,fcntl.LOCK_EX|fcntl.LOCK_NB)
backup=base/"price_sync_backups"/datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S")
backup.mkdir(parents=True,exist_ok=True)
control=state/"price_sync_control.json"
if control.exists(): shutil.copy2(control,backup/"price_sync_control.json")
# Pause writes before replacing files, while holding the task's shared lock.
control.write_text(json.dumps({"schema_version":1,"write_enabled":False,"reason":"awaiting_service_key_validation"})+"\\n")
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
print(json.dumps({"files_deployed":count,"backup":str(backup),"write_enabled":False}))
'''
    print(remote_python(code,stdin=base64.b64encode(stream.getvalue()),host=host))


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--host",default="firecrawl")
    parser.add_argument("--include-credentials",action="store_true")
    args=parser.parse_args()
    deploy(args.host,args.include_credentials)


if __name__ == "__main__":
    main()
