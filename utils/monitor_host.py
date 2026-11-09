"""监控主机（ssh 别名 firecrawl，Windows 上的 WSL）：经 ssh 在 WSL 里执行 Python，拉回待执行 SQL，把已执行的文件移入 applied/。"""
from __future__ import annotations

import base64
import gzip
import json
import subprocess
from pathlib import Path

from utils.pending_sql import APPLIED_DIR_NAME, PENDING_DIR_NAME

HOST = "firecrawl"
BASE = "/home/loong/costfinder_monitor"
# 每次 ssh 调用取回的文件内容上限（字节）
PULL_BATCH_BYTES = 1_000_000


def remote_python(code: str, *, stdin: bytes = b"", timeout: int = 60, host: str = HOST) -> str:
    encoded = base64.b64encode(code.encode()).decode()
    command = 'wsl -e python3 -c "import base64;exec(base64.b64decode(\'' + encoded + '\'))"'
    result = subprocess.run(["ssh", "-o", "BatchMode=yes", "-o", "ConnectTimeout=10", host, command],
                            input=stdin, capture_output=True, timeout=timeout)
    if result.returncode:
        raise RuntimeError("remote_operation_failed:" + result.stderr.decode(errors="replace")[-300:])
    return result.stdout.decode()


def _remote_pending(base: str) -> str:
    return f"{base}/state/{PENDING_DIR_NAME}"


def _remote_listing(base: str, host: str) -> dict[str, int]:
    """服务器上待执行文件的相对路径与字节数。"""
    code = (f"import json,sys\nfrom pathlib import Path\nsys.path.insert(0,{base!r})\n"
            "from utils.pending_sql import list_pending\n"
            f"root=Path({_remote_pending(base)!r})\n"
            "found=list_pending(root) if root.is_dir() else []\n"
            "print(json.dumps({str(p.relative_to(root)):p.stat().st_size for p,_ in found}))\n")
    return json.loads(remote_python(code, host=host))


def _batches(sizes: dict[str, int]) -> list[list[str]]:
    """按 PULL_BATCH_BYTES 分批；单个超过上限的文件独占一批。"""
    batches: list[list[str]] = []
    total = 0
    for relative, size in sorted(sizes.items()):
        if not batches or total + size > PULL_BATCH_BYTES:
            batches.append([])
            total = 0
        batches[-1].append(relative)
        total += size
    return batches


def pull_pending(local_dir: Path, *, base: str = BASE, host: str = HOST) -> list[Path]:
    """把服务器 pending_sql/ 里待执行的文件拉到本地同名路径；本地已有或已移入 applied/ 的不传输。
    先取清单，再按大小分批取内容：一次传回全部内容经 Windows OpenSSH 与 WSL 会超过几分钟。"""
    local_dir = Path(local_dir)
    missing = {relative: size for relative, size in _remote_listing(base, host).items()
               if not (local_dir / relative).exists()
               and not (local_dir / APPLIED_DIR_NAME / Path(relative).parts[0]).exists()}
    written = []
    for batch in _batches(missing):
        # 内容经 gzip 压缩后传回：ssh 到 WSL 的标准输出在 2MB 以上会被截断，SQL 文本压缩后不到原来的十分之一
        code = (f"import base64,gzip,json\nfrom pathlib import Path\nroot=Path({_remote_pending(base)!r})\n"
                f"print(json.dumps({{r:base64.b64encode(gzip.compress((root/r).read_bytes())).decode() "
                f"for r in {batch!r}}}))\n")
        for relative, content in json.loads(remote_python(code, host=host, timeout=300)).items():
            target = local_dir / relative
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(gzip.decompress(base64.b64decode(content)))
            written.append(target)
    return written


def mark_applied_remote(relative: str, *, base: str = BASE, host: str = HOST) -> str:
    """在服务器上对同一个文件执行 mark_applied；下一次 monitor 运行时对应事件变为 completed。"""
    code = (f"import sys\nfrom pathlib import Path\nsys.path.insert(0,{base!r})\n"
            "from utils.pending_sql import mark_applied\n"
            f"print(mark_applied(Path({_remote_pending(base)!r})/{relative!r}))\n")
    return remote_python(code, host=host).strip()
