from __future__ import annotations

import io
import sys
import tarfile

from scripts import pending_sql as command
from utils import pending_sql_sync
from utils.pending_sql import list_pending, write_pending
from utils.pending_sql_sync import MONITOR_PENDING, pull_command, remote_mark_applied_command


def test_pull_reads_the_monitor_pending_directory_over_ssh_without_applied_files():
    cmd = pull_command()
    assert cmd[:2] == ["ssh", "-o"] and "firecrawl" in cmd
    assert cmd[-1] == f"wsl -e tar -C {MONITOR_PENDING} --exclude=./applied -cf - ."


def test_remote_mark_applied_moves_the_same_file_on_the_monitor_host():
    cmd = remote_mark_applied_command("20261107T010203000000Z_evt1.sql")
    assert cmd[-1] == (f"wsl -e sh -c 'mkdir -p {MONITOR_PENDING}/applied && "
                       f"mv {MONITOR_PENDING}/20261107T010203000000Z_evt1.sql {MONITOR_PENDING}/applied/'")


def test_listing_pulls_first_and_mark_applied_registers_both_sides(tmp_path, monkeypatch, capsys):
    mirror = tmp_path / "pending_sql"
    remote = tmp_path / "remote"
    path = write_pending(remote, "evt1", "d" * 64, {"source_url": "https://a.example"}, "do $pipeline$ begin end $pipeline$;\n")
    calls = []

    def fake_run(cmd, **kwargs):
        # 代替 ssh：拉取时返回监控主机目录的 tar 流，登记时只记录命令
        calls.append(cmd)
        buffer = io.BytesIO()
        with tarfile.open(fileobj=buffer, mode="w") as archive:
            archive.add(path, arcname=f"./{path.name}")
        return type("Done", (), {"stdout": buffer.getvalue(), "returncode": 0})()

    monkeypatch.setattr(pending_sql_sync.subprocess, "run", fake_run)
    monkeypatch.setattr(command, "MONITOR_MIRROR", mirror)
    monkeypatch.setattr(sys, "argv", ["pending-sql", "--dir", str(mirror)])
    command.main()
    assert calls == [pull_command()] and [p.name for p, _ in list_pending(mirror)] == [path.name]
    monkeypatch.setattr(sys, "argv", ["pending-sql", "--mark-applied", str(mirror / path.name)])
    command.main()
    assert calls[-1] == remote_mark_applied_command(path.name)
    assert (mirror / "applied" / path.name).is_file() and list_pending(mirror) == []


def test_files_from_the_firecrawl_outage_window_are_not_listed(tmp_path):
    directory = tmp_path / "pending_sql"
    directory.mkdir()
    (directory / "20261107T010000000000Z_evt1.sql").write_text("-- plan evt1\n")
    (directory / "20261108T010000000000Z_evt2.sql").write_text("-- plan evt2\n")
    assert [p.name for p, _ in list_pending(directory)] == ["20261108T010000000000Z_evt2.sql"]
