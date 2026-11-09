import fcntl
import shutil
import subprocess

import pytest

from scripts.deploy_monitor_price_sync import BASE, monitor_launcher


def _launcher(tmp_path, **kwargs) -> str:
    return monitor_launcher("retry.log", "failure_retry", " --retry-transient-only", **kwargs).decode().replace(
        BASE, str(tmp_path))


def test_only_the_retry_launcher_yields_to_a_running_monitor():
    assert "flock -n" in monitor_launcher("retry.log", "failure_retry", "", yield_to_running=True).decode()
    assert "flock" not in monitor_launcher("monitor.log", "full_monitor", "").decode()


@pytest.mark.skipif(shutil.which("flock") is None, reason="flock(1) is part of util-linux on the monitor host")
def test_retry_exits_quietly_while_the_full_monitor_holds_the_lock(tmp_path):
    script = tmp_path / "run_monitor_retry.sh"
    script.write_text(_launcher(tmp_path, yield_to_running=True))
    (tmp_path / "state").mkdir()
    with (tmp_path / "state" / ".monitor.lock").open("a") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        result = subprocess.run(["sh", str(script)], capture_output=True)
    assert result.returncode == 0
    log = (tmp_path / "logs" / "retry.log").read_text()
    assert "monitor running, skipped" in log
    assert "promo_pipeline" not in log


@pytest.mark.skipif(shutil.which("flock") is None, reason="flock(1) is part of util-linux on the monitor host")
def test_retry_runs_when_no_monitor_holds_the_lock(tmp_path):
    script = tmp_path / "run_monitor_retry.sh"
    script.write_text(_launcher(tmp_path, yield_to_running=True))
    result = subprocess.run(["sh", str(script)], capture_output=True)
    # 测试目录里没有 .venv，monitor 启动失败；说明锁空闲时没有被跳过
    assert result.returncode != 0
    assert "monitor running, skipped" not in (tmp_path / "logs" / "retry.log").read_text()


def test_deploy_leaves_the_server_control_file_alone(monkeypatch):
    from scripts import deploy_monitor_price_sync as deploy
    sent = []
    monkeypatch.setattr(deploy, "remote_python", lambda code, **kwargs: sent.append(code) or "{}")
    deploy.deploy("firecrawl", False)
    # 写入只生成待执行 SQL，部署不再暂停写入、不再改写 price_sync_control.json
    assert len(sent) == 1 and "price_sync_control" not in sent[0] and "write_enabled" not in sent[0]
