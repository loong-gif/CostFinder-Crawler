from __future__ import annotations

import io
import subprocess
import tarfile
from pathlib import Path

MONITOR_HOST = "firecrawl"
MONITOR_PENDING = "/home/loong/costfinder_monitor/state/pending_sql"
SSH = ["ssh", "-o", "BatchMode=yes", "-o", "ConnectTimeout=10", MONITOR_HOST]


def pull_command() -> list[str]:
    """监控主机（WSL）上待执行 SQL 目录的 tar 流，不含 applied/。"""
    return [*SSH, f"wsl -e tar -C {MONITOR_PENDING} --exclude=./applied -cf - ."]


def remote_mark_applied_command(name: str) -> list[str]:
    """在监控主机上把同名文件（或 <ts>/ 目录）移入 applied/，下次 monitor 运行时事件即为 completed。"""
    return [*SSH, f"wsl -e sh -c 'mkdir -p {MONITOR_PENDING}/applied && "
                  f"mv {MONITOR_PENDING}/{name} {MONITOR_PENDING}/applied/'"]


def pull_pending(local_dir: Path) -> None:
    """把监控主机的待执行 SQL 拉到本地镜像目录。"""
    archive = subprocess.run(pull_command(), capture_output=True, check=True).stdout
    Path(local_dir).mkdir(parents=True, exist_ok=True)
    with tarfile.open(fileobj=io.BytesIO(archive)) as tar:
        tar.extractall(local_dir, filter="data")


def remote_mark_applied(name: str) -> None:
    subprocess.run(remote_mark_applied_command(name), capture_output=True, check=True)
