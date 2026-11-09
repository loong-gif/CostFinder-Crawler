"""监控主机（ssh 别名 firecrawl，Windows 上的 WSL）：经 ssh 在 WSL 里执行 Python，拉回待执行 SQL，把已执行的文件移入 applied/。"""
from __future__ import annotations

import base64
import json
import socket
import subprocess
from pathlib import Path

from utils.pending_sql import APPLIED_DIR_NAME, PENDING_DIR_NAME

HOST = "firecrawl"
BASE = "/home/loong/costfinder_monitor"


def remote_python(code: str, *, stdin: bytes = b"", timeout: int = 60, host: str = HOST) -> str:
    encoded = base64.b64encode(code.encode()).decode()
    command = 'wsl -e python3 -c "import base64;exec(base64.b64decode(\'' + encoded + '\'))"'
    result = subprocess.run(["ssh", "-o", "BatchMode=yes", "-o", "ConnectTimeout=10", host, command],
                            input=stdin, capture_output=True, timeout=timeout)
    if result.returncode:
        raise RuntimeError("remote_operation_failed:" + result.stderr.decode(errors="replace")[-300:])
    return result.stdout.decode()


def ensure_tunnel(local_port: int, remote_port: int, *, host: str = HOST) -> bool:
    """本机端口没有在监听时，后台建立到监控主机本机端口的 ssh 隧道；返回是否新建了隧道。"""
    with socket.socket() as probe:
        if probe.connect_ex(("127.0.0.1", local_port)) == 0:
            return False
    subprocess.run(["ssh", "-f", "-N", "-o", "BatchMode=yes", "-o", "ExitOnForwardFailure=yes",
                    "-L", f"127.0.0.1:{local_port}:127.0.0.1:{remote_port}", host], check=True, timeout=30)
    return True


def _remote_pending(base: str) -> str:
    return f"{base}/state/{PENDING_DIR_NAME}"


def pull_pending(local_dir: Path, *, base: str = BASE, host: str = HOST) -> list[Path]:
    """把服务器 pending_sql/ 里待执行的文件拉到本地同名路径；本地已有或已移入 applied/ 的不再写入。"""
    code = (f"import base64,json,sys\nfrom pathlib import Path\nsys.path.insert(0,{base!r})\n"
            "from utils.pending_sql import list_pending\n"
            f"root=Path({_remote_pending(base)!r})\n"
            "found=list_pending(root) if root.is_dir() else []\n"
            "print(json.dumps({str(p.relative_to(root)):base64.b64encode(p.read_bytes()).decode() for p,_ in found}))\n")
    files = json.loads(remote_python(code, host=host, timeout=300))
    local_dir = Path(local_dir)
    written = []
    for relative, content in sorted(files.items()):
        target = local_dir / relative
        applied = local_dir / APPLIED_DIR_NAME / Path(relative).parts[0]
        if target.exists() or applied.exists():
            continue
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(base64.b64decode(content))
        written.append(target)
    return written


def mark_applied_remote(relative: str, *, base: str = BASE, host: str = HOST) -> str:
    """在服务器上对同一个文件执行 mark_applied；下一次 monitor 运行时对应事件变为 completed。"""
    code = (f"import sys\nfrom pathlib import Path\nsys.path.insert(0,{base!r})\n"
            "from utils.pending_sql import mark_applied\n"
            f"print(mark_applied(Path({_remote_pending(base)!r})/{relative!r}))\n")
    return remote_python(code, host=host).strip()
