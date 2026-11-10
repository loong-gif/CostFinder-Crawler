from __future__ import annotations

import asyncio
import base64
import json
import plistlib
import re
import subprocess
import sys
import uuid
from pathlib import Path

import pgserver
import psycopg
import pytest
from mcp import Client
from mcp.server.mcpserver import MCPServer
from mcp.server.mcpserver.exceptions import ToolError

from utils import monitor_host
from utils import pending_sql_apply as apply
from utils.monitor_pending_sql import settle
from utils.pending_sql import write_pending

ROOT = Path(__file__).resolve().parents[1]
REAL_RUN = subprocess.run


def server(tmp_path):
    """监控主机的部署目录：仓库的 utils/ 加 state/，远程程序从这里导入。"""
    base = tmp_path / "server"
    base.mkdir()
    (base / "utils").symlink_to(ROOT / "utils")
    return base


def local_ssh(cmd, input=b"", capture_output=True, timeout=None):
    """ssh 换成在本机执行同一段远程程序。"""
    code = base64.b64decode(re.search(r"b64decode\('([^']+)'\)", cmd[-1]).group(1)).decode()
    return REAL_RUN([sys.executable, "-c", code], input=input, capture_output=True, timeout=timeout)
# 与生产 admin_audit_log 相同的列；prices 代表待执行 SQL 写入的业务表
SCHEMA = """
drop table if exists public.admin_audit_log, public.prices;
create table public.admin_audit_log (id uuid primary key default gen_random_uuid(), admin_id text not null,
  admin_email text, action text not null, target_type text not null, target_id text, details jsonb,
  created_at timestamptz default now());
create table public.prices (id int primary key, price int not null);
insert into public.prices values (1, 100), (2, 200), (3, 300);
"""


@pytest.fixture(scope="module")
def database(tmp_path_factory):
    return pgserver.get_server(tmp_path_factory.mktemp("pg")).get_uri()


def mcp_server(database, *, read_only=False):
    """本机 PostgreSQL 上的 MCP 服务：execute_sql 与托管 Supabase MCP 相同，一次调用的全部语句在一个事务里执行；
    只读令牌对应的服务在只读事务里执行。"""
    mcp = MCPServer("supabase")

    @mcp.tool()
    def execute_sql(query: str) -> str:
        with psycopg.connect(database) as conn:
            if read_only:
                conn.execute("set transaction read only")
            # 与托管服务相同：数据库错误原文与 SQLSTATE 作为工具错误返回
            try:
                conn.execute(query)
            except psycopg.Error as exc:
                raise ToolError(f"Failed to run sql query: ERROR:  {exc.sqlstate}: {exc}") from exc
        return "ok"

    return mcp


@pytest.fixture
def supabase(database):
    with psycopg.connect(database, autocommit=True) as conn:
        conn.execute(SCHEMA)
    return mcp_server(database)


def query(database, sql):
    with psycopg.connect(database, autocommit=True) as conn:
        return conn.execute(sql).fetchall()


def execute(database, sql):
    with psycopg.connect(database, autocommit=True) as conn:
        conn.execute(sql)


def prices(database):
    return dict(query(database, "select id, price from public.prices order by id"))


def remote_file(base, plan_id, sql):
    return write_pending(base / "state", plan_id, "a" * 64, {"url": "https://a.test"}, sql)


def guarded(row, before, after):
    """与监控 DO 块相同的形状：before 不符即 raise，整个文件回滚。"""
    return (f"do $pipeline$ begin\n  if (select price from public.prices where id = {row}) <> {before} "
            f"then raise exception 'stale_before: prices {row}'; end if;\n"
            f"  update public.prices set price = {after} where id = {row};\nend\n$pipeline$;\n")


def run(supabase, local, base):
    async def go():
        async with Client(supabase) as client:
            return await apply.apply_pulled(local, client, base=str(base))
    return asyncio.run(go())


@pytest.fixture
def host(tmp_path, monkeypatch):
    monkeypatch.setattr(monitor_host.subprocess, "run", local_ssh)
    return server(tmp_path)


def test_pulled_files_run_in_filename_order_and_are_marked_applied_on_both_sides(tmp_path, host, supabase, database):
    first = remote_file(host, "evt1", guarded(1, 100, 90))
    nested = host / "state" / "pending_sql" / (first.name[:15] + "9Z") / "apply.sql"
    nested.parent.mkdir()
    # 第二个文件依赖第一个文件的结果：顺序颠倒会 raise
    nested.write_text("-- deactivate expired\n" + guarded(1, 90, 80))
    local = tmp_path / "pending_sql"
    report = run(supabase, local, host)
    assert report == {"applied": [local / first.name, local / nested.parent.name / "apply.sql"],
                      "rejected": [], "stopped": None, "error": None}
    assert prices(database)[1] == 80
    remote_applied = host / "state" / "pending_sql" / "applied"
    assert (remote_applied / first.name).is_file() and (remote_applied / nested.parent.name / "apply.sql").is_file()
    assert (local / "applied" / first.name).is_file() and (local / "applied" / nested.parent.name).is_dir()
    assert query(database, "select admin_id, action, target_type, target_id from public.admin_audit_log "
                           "order by target_id") == [
        ("automated_data_repair", "pending_sql_applied", "pending_sql", first.name),
        ("automated_data_repair", "pending_sql_applied", "pending_sql", f"{nested.parent.name}/apply.sql")]


def test_file_rejected_by_its_own_guard_moves_to_rejected_and_the_batch_continues(tmp_path, host, supabase, database):
    ok = remote_file(host, "evt1", guarded(1, 100, 90))
    stale = remote_file(host, "evt2", guarded(2, 999, 1))
    later = remote_file(host, "evt3", guarded(3, 300, 30))
    local = tmp_path / "pending_sql"
    text = stale.read_text()
    report = run(supabase, local, host)
    assert report == {"applied": [local / ok.name, local / later.name], "rejected": [(local / stale.name, "stale_before")],
                      "stopped": None, "error": None}
    # DO 块整体回滚，被拒文件没有写入
    assert prices(database) == {1: 90, 2: 200, 3: 30}
    for root in (host / "state" / "pending_sql", local):
        assert not (root / stale.name).exists() and not (root / "applied" / stale.name).exists()
        assert (root / "rejected" / stale.name).read_text() == text
        assert "stale_before: prices 2" in (root / "rejected" / (stale.name + ".error.txt")).read_text()
    # 监控把移出 pending_sql/ 的文件视为缺失：事件回到 retryable，按新证据重新规划
    assert settle({"sql_path": str(stale)}, host / "state") == "retryable"
    assert settle({"sql_path": str(ok)}, host / "state") == "completed"
    assert run(supabase, local, host) == {"applied": [], "rejected": [], "stopped": None, "error": None}
    assert query(database, "select count(*) from public.admin_audit_log") == [(2,)]


def test_read_only_token_stops_the_batch_and_leaves_the_file_pending(tmp_path, host, supabase, database):
    first = remote_file(host, "evt1", guarded(1, 100, 90))
    later = remote_file(host, "evt2", guarded(2, 200, 20))
    local = tmp_path / "pending_sql"
    report = run(mcp_server(database, read_only=True), local, host)
    assert report["applied"] == [] and report["rejected"] == [] and report["stopped"] == local / first.name
    assert "25006" in report["error"] and apply.READ_ONLY_TOKEN in report["error"]
    assert "25006" in (local / (first.name + ".error.txt")).read_text()
    assert first.is_file() and later.is_file() and (local / first.name).is_file() and (local / later.name).is_file()
    assert not (local / "rejected").exists() and prices(database) == {1: 100, 2: 200, 3: 300}
    # 换成可写令牌后执行，错误文件随之删除
    assert run(supabase, local, host)["applied"] == [local / first.name, local / later.name]
    assert not (local / (first.name + ".error.txt")).exists() and not (local / "applied" / (first.name + ".error.txt")).exists()


def test_errors_in_the_hosted_service_format_are_classified():
    # 托管服务把错误包在 JSON 里，换行是转义后的 \n
    def hosted(message):
        return json.dumps({"error": {"name": "HttpException", "message": f"Failed to run sql query: ERROR:  {message}\n"}})
    guard = hosted("P0001: write_not_one_row\nCONTEXT:  PL/pgSQL function inline_code_block line 9 at RAISE")
    assert apply.outcome(True, guard) == "rejected" and apply.guard_name(guard) == "write_not_one_row"
    stale = hosted("P0001: stale_plan: item 7 no longer has its planned service")
    assert apply.guard_name(stale) == "stale_plan"
    column = hosted('42703: column "id" does not exist\nQUERY:  update clinic_promotions set id = id')
    assert apply.outcome(True, column) == "rejected" and apply.guard_name(column) == "42703"
    read_only = hosted("25006: cannot execute UPDATE in a read-only transaction")
    assert apply.outcome(True, read_only) == "stopped"
    assert apply.outcome(True, '{"error":{"name":"Unauthorized","message":"Unauthorized"}}') == "stopped"
    assert apply.outcome(False, "[]") == "applied"
    assert apply.outcome(True, hosted("P0001: pending_sql_already_applied: a.sql")) == "applied"


def test_error_that_is_not_a_sql_error_stops_the_batch(tmp_path, host, database):
    path = remote_file(host, "evt1", guarded(1, 100, 90))
    local = tmp_path / "pending_sql"
    report = run(MCPServer("supabase"), local, host)
    assert report["stopped"] == local / path.name and report["rejected"] == [] and path.is_file()
    assert "execute_sql" in report["error"]


def test_rerun_after_a_crash_between_commit_and_marking_does_not_execute_twice(tmp_path, host, supabase, database):
    path = remote_file(host, "evt1", "update public.prices set price = price - 1 where id = 1;\n")
    local = tmp_path / "pending_sql"
    monitor_host.pull_pending(local, base=str(host))

    async def commit_only():
        async with Client(supabase) as client:
            return await apply.execute_file(client, local / path.name, local)
    # 事务已提交，进程在登记前终止
    assert asyncio.run(commit_only()) == ("applied", "ok")
    assert run(supabase, local, host)["applied"] == [local / path.name]
    assert prices(database)[1] == 99
    assert (host / "state" / "pending_sql" / "applied" / path.name).is_file()
    assert query(database, "select count(*) from public.admin_audit_log") == [(1,)]


def test_rerun_after_a_crash_between_the_server_move_and_the_local_move(tmp_path, host, supabase, database):
    path = remote_file(host, "evt1", guarded(1, 100, 90))
    local = tmp_path / "pending_sql"
    run(supabase, local, host)
    # 本地文件回到待执行：服务器已移入 applied/，本地移动前进程终止
    (local / "applied" / path.name).rename(local / path.name)
    assert run(supabase, local, host) == {"applied": [local / path.name], "rejected": [], "stopped": None, "error": None}
    assert (local / "applied" / path.name).is_file() and prices(database)[1] == 90


def test_changed_file_content_is_executed_again(tmp_path, host, supabase, database):
    path = remote_file(host, "evt1", "update public.prices set price = price - 1 where id = 1;\n")
    local = tmp_path / "pending_sql"
    monitor_host.pull_pending(local, base=str(host))

    async def go():
        async with Client(supabase) as client:
            first = await apply.execute_file(client, local / path.name, local)
            (local / path.name).write_text("update public.prices set price = price - 10 where id = 1;\n")
            return first, await apply.execute_file(client, local / path.name, local)
    assert asyncio.run(go()) == (("applied", "ok"), ("applied", "ok"))
    assert prices(database)[1] == 89


def test_file_with_transaction_control_is_rejected_before_execution(tmp_path):
    path = write_pending(tmp_path, "evt1", "a" * 64, {}, "begin;\nupdate public.prices set price = 1;\ncommit;\n")
    with pytest.raises(ValueError, match="transaction_control_in_pending_sql"):
        apply.guarded_sql(path, path.parent)


def test_unsafe_relative_name_is_rejected(tmp_path):
    path = tmp_path / "a'b.sql"
    path.write_text("select 1;\n")
    with pytest.raises(ValueError, match="invalid_pending_sql_name"):
        apply.guarded_sql(path, tmp_path)


def test_missing_keychain_item_fails_fast_with_instructions():
    service = f"costfinder-test-missing-{uuid.uuid4().hex}"
    with pytest.raises(RuntimeError) as error:
        apply.keychain_token(service)
    assert f"security add-generic-password -U -a costfinder -s {service} -w" in str(error.value)


def test_session_connects_directly_even_when_the_shell_sets_a_proxy(monkeypatch):
    # 本机代理会中断 Python 的 TLS 握手，launchd 任务也没有代理变量：手动与定时运行都直连 MCP 服务
    from http.server import BaseHTTPRequestHandler, HTTPServer
    from threading import Thread

    received = []

    class Unauthorized(BaseHTTPRequestHandler):
        def do_POST(self):
            received.append(self.headers["Authorization"])
            self.send_response(401)
            self.send_header("Content-Length", "0")
            self.end_headers()

        def log_message(self, *args):
            pass

    server = HTTPServer(("127.0.0.1", 0), Unauthorized)
    Thread(target=server.serve_forever, daemon=True).start()
    for name in ("HTTP_PROXY", "HTTPS_PROXY", "ALL_PROXY", "http_proxy", "https_proxy", "all_proxy"):
        monkeypatch.setenv(name, "http://127.0.0.1:9")
    for name in ("NO_PROXY", "no_proxy"):
        monkeypatch.delenv(name, raising=False)

    async def go():
        async with apply.supabase_session("token", url=f"http://127.0.0.1:{server.server_port}/mcp"):
            pass

    with pytest.raises(BaseException):
        asyncio.run(go())
    server.shutdown()
    assert received and received[0] == "Bearer token"


def test_launchd_agent_runs_the_apply_hourly_at_minute_50_with_logs_in_the_repository():
    path = ROOT / "config" / "launchd" / f"{apply.LAUNCHD_LABEL}.plist"
    assert subprocess.run(["plutil", "-lint", str(path)], capture_output=True).returncode == 0
    agent = plistlib.loads(path.read_bytes())
    assert agent["Label"] == apply.LAUNCHD_LABEL
    assert agent["ProgramArguments"] == [str(ROOT / ".venv" / "bin" / "python3"),
                                         str(ROOT / "scripts" / "promo_pipeline.py"), "pending-sql", "--apply"]
    assert agent["WorkingDirectory"] == str(ROOT)
    assert agent["StartCalendarInterval"] == {"Minute": 50}
    log_dir = ROOT / "output" / "launchd"
    assert agent["StandardOutPath"] == str(log_dir / "pending_sql_apply.log")
    assert agent["StandardErrorPath"] == str(log_dir / "pending_sql_apply.log")
    assert subprocess.run(["git", "check-ignore", "-q", str(log_dir / "pending_sql_apply.log")], cwd=ROOT).returncode == 0


def test_install_copies_the_repository_plist_and_creates_the_log_directory(tmp_path):
    source = ROOT / "config" / "launchd" / f"{apply.LAUNCHD_LABEL}.plist"
    installed = apply.install_plist(tmp_path / "LaunchAgents")
    assert installed == tmp_path / "LaunchAgents" / source.name
    assert installed.read_bytes() == source.read_bytes()
    assert (ROOT / "output" / "launchd").is_dir()
