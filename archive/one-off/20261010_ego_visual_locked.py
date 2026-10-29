"""Serialize browser rounds in the shared visual-review TaskSpace.

Usage: .venv/bin/python one-off/20261010_ego_visual_locked.py < round.js
The caller still owns its Page; this only serializes navigation and capture.
"""

import fcntl
import os
from pathlib import Path
import signal
import subprocess
import sys


ROOT = Path(__file__).resolve().parents[1]
LOCK = ROOT / "output/monitor_price_sync/visual_review_20261010/.browser_round.lock"


def main():
    script = sys.stdin.buffer.read()
    if not script.strip():
        raise SystemExit("Browser script required on stdin")
    LOCK.parent.mkdir(parents=True, exist_ok=True)
    with LOCK.open("a") as handle:
        fcntl.flock(handle, fcntl.LOCK_EX)
        child = subprocess.Popen(
            ["ego-browser", "nodejs"],
            stdin=subprocess.PIPE,
            start_new_session=True,
        )
        try:
            child.communicate(script, timeout=90)
        except subprocess.TimeoutExpired:
            os.killpg(child.pid, signal.SIGTERM)
            try:
                child.wait(timeout=5)
            except subprocess.TimeoutExpired:
                os.killpg(child.pid, signal.SIGKILL)
                child.wait()
            print("Browser round exceeded 90 seconds; child process stopped", file=sys.stderr)
            return 124
        return child.returncode


if __name__ == "__main__":
    raise SystemExit(main())
