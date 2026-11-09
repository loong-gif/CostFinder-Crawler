"""pending-sql 与监控主机之间的拉回和已执行登记：ssh 子进程换成在本机执行同一段远程程序，服务器目录用临时目录代替。"""
from __future__ import annotations

import base64
import json
import re
import subprocess
import sys
from pathlib import Path

import pytest

from scripts import pending_sql as command
from scripts.run_monitor_price_sync import process_events
from utils import monitor_host
from utils.monitor_price_store import atomic_json
from utils.pending_sql import write_pending

ROOT = Path(__file__).resolve().parents[1]
REAL_RUN = subprocess.run


def server(tmp_path):
    base = tmp_path / "server"
    base.mkdir()
    # 服务器部署目录里有仓库的 utils/，远程程序从那里导入
    (base / "utils").symlink_to(ROOT / "utils")
    return base


def fake_ssh(calls, *, fail=False):
    def run(cmd, input=b"", capture_output=True, timeout=None):
        calls.append(cmd)
        assert cmd[:6] == ["ssh", "-o", "BatchMode=yes", "-o", "ConnectTimeout=10", "firecrawl"]
        assert cmd[6].startswith('wsl -e python3 -c "import base64;exec(base64.b64decode(')
        code = base64.b64decode(re.search(r"b64decode\('([^']+)'\)", cmd[6]).group(1)).decode()
        if fail:
            return subprocess.CompletedProcess(cmd, 255, b"", b"ssh: connect to host firecrawl: timed out")
        return REAL_RUN([sys.executable, "-c", code], input=input, capture_output=True, timeout=timeout)
    return run


def run_command(monkeypatch, *args):
    monkeypatch.setattr("sys.argv", ["pending-sql", *args])
    return command.main()


def test_pull_copies_every_pending_file_once_and_skips_applied_ones(tmp_path, monkeypatch):
    base = server(tmp_path)
    state = base / "state"
    flat = write_pending(state, "evt1", "a" * 64, {"url": "https://a.test"}, "select 1;\n")
    nested = state / "pending_sql" / "20261109T000000Z" / "apply.sql"
    nested.parent.mkdir(parents=True)
    nested.write_text("-- deactivate expired\nselect 2;\n")
    done = write_pending(state, "evt0", "b" * 64, {}, "select 0;\n")
    (state / "pending_sql" / "applied").mkdir()
    done.rename(state / "pending_sql" / "applied" / done.name)
    calls = []
    monkeypatch.setattr(monitor_host.subprocess, "run", fake_ssh(calls))
    local = tmp_path / "local"
    assert run_command(monkeypatch, "--dir", str(local), "--remote-base", str(base)) == 0
    assert (local / flat.name).read_text() == flat.read_text()
    assert (local / "20261109T000000Z" / "apply.sql").read_text() == nested.read_text()
    # 一次列出文件清单，一次取回两个文件的内容
    assert not (local / done.name).exists() and len(calls) == 2
    # 本地已登记执行的文件不再拉回，也不再传输内容
    (local / "applied").mkdir()
    (local / flat.name).rename(local / "applied" / flat.name)
    calls.clear()
    assert monitor_host.pull_pending(local, base=str(base)) == []
    assert not (local / flat.name).exists() and len(calls) == 1


def test_large_pending_sets_are_fetched_in_batches(tmp_path, monkeypatch):
    base = server(tmp_path)
    state = base / "state"
    paths = [write_pending(state, f"evt{i}", str(i) * 64, {}, f"select {i};\n" + "-- pad\n" * 200) for i in range(3)]
    calls = []
    monkeypatch.setattr(monitor_host.subprocess, "run", fake_ssh(calls))
    monkeypatch.setattr(monitor_host, "PULL_BATCH_BYTES", paths[0].stat().st_size)
    local = tmp_path / "local"
    written = monitor_host.pull_pending(local, base=str(base))
    assert sorted(p.name for p in written) == sorted(p.name for p in paths)
    assert all((local / p.name).read_text() == p.read_text() for p in paths)
    # 一次清单，每批一个文件
    assert len(calls) == 4


def test_files_from_the_firecrawl_outage_window_are_neither_pulled_nor_listed(tmp_path, monkeypatch, capsys):
    base = server(tmp_path)
    remote = base / "state" / "pending_sql"
    remote.mkdir(parents=True)
    (remote / "20261107T010000000000Z_evt1.sql").write_text("-- plan evt1\n")
    (remote / "20261108T010000000000Z_evt2.sql").write_text("-- plan evt2\n")
    monkeypatch.setattr(monitor_host.subprocess, "run", fake_ssh([]))
    local = tmp_path / "local"
    local.mkdir()
    (local / "20261106T200000000000Z_evt0.sql").write_text("-- plan evt0\n")
    assert run_command(monkeypatch, "--dir", str(local), "--remote-base", str(base)) == 0
    assert sorted(p.name for p in local.iterdir()) == ["20261106T200000000000Z_evt0.sql",
                                                       "20261108T010000000000Z_evt2.sql"]
    out = capsys.readouterr().out
    assert f"{local}: 1 pending" in out and "evt0" not in out


def test_mark_applied_moves_the_file_on_the_server_so_the_monitor_event_completes(tmp_path, monkeypatch):
    base = server(tmp_path)
    state = base / "state"
    sql = write_pending(state, "evt1", "a" * 64, {"url": "https://a.test"}, "select 1;\n")
    atomic_json(state / "events" / "evt1.json", {"event_id": "evt1", "status": "pending_sql", "url": "https://a.test",
                                                "created_at": "1", "receipt": {"status": "pending_sql", "sql_path": str(sql)}})
    calls = []
    monkeypatch.setattr(monitor_host.subprocess, "run", fake_ssh(calls))
    local = tmp_path / "local"
    run_command(monkeypatch, "--dir", str(local), "--remote-base", str(base))
    assert run_command(monkeypatch, "--mark-applied", str(local / sql.name), "--dir", str(local),
                       "--remote-base", str(base)) == 0
    assert (local / "applied" / sql.name).is_file() and (state / "pending_sql" / "applied" / sql.name).is_file()
    # 拉回（清单、内容）与服务器端登记
    assert not sql.exists() and len(calls) == 3
    process_events(state, {})
    assert json.loads((state / "events" / "evt1.json").read_text())["status"] == "completed"


def test_failed_server_move_keeps_the_local_file_pending(tmp_path, monkeypatch):
    local = tmp_path / "local"
    path = write_pending(tmp_path / "unused", "evt1", "a" * 64, {}, "select 1;\n")
    local.mkdir()
    path = path.rename(local / path.name)
    monkeypatch.setattr(monitor_host.subprocess, "run", fake_ssh([], fail=True))
    with pytest.raises(RuntimeError, match="remote_operation_failed"):
        run_command(monkeypatch, "--mark-applied", str(path), "--dir", str(local))
    assert path.is_file() and not (local / "applied").exists()


def test_file_outside_the_pulled_directory_is_marked_locally_only(tmp_path, monkeypatch):
    own = write_pending(tmp_path / "data_quality_state", "dq1", "c" * 64, {}, "select 3;\n")
    monkeypatch.setattr(monitor_host.subprocess, "run", lambda *a, **k: pytest.fail("local file needs no ssh"))
    assert run_command(monkeypatch, "--mark-applied", str(own), "--dir", str(tmp_path / "local")) == 0
    assert (own.parent / "applied" / own.name).is_file()
