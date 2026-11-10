"""本机无人值守执行待执行 SQL：拉回监控主机的文件，按文件名顺序经托管 Supabase MCP 的 execute_sql 逐个执行并登记已执行。"""
from __future__ import annotations

import hashlib
import os
import re
import shutil
import subprocess
from contextlib import asynccontextmanager
from pathlib import Path
from typing import AsyncIterator, Callable

from mcp import Client
from mcp.client.streamable_http import streamable_http_client
from mcp.shared._httpx_utils import create_mcp_http_client
from pglast import ast, parse_sql

from utils.monitor_host import BASE, HOST, mark_applied_remote, mark_rejected_remote, pull_pending
from utils.pending_sql import list_pending, mark_applied, mark_rejected
from utils.pipeline_run import PROJECT_ROOT

SUPABASE_MCP_URL = "https://mcp.supabase.com/mcp?project_ref=kdlpkjzcnbkjcvwsvlwn&features=database"
KEYCHAIN_SERVICE = "costfinder-supabase-write"
LAUNCHD_LABEL = "com.costfinder.pending-sql-apply"
# 已执行登记沿用 admin_audit_log 中自动数据修复的写法；与 SQL 文件在同一个事务里提交
AUDIT_ADMIN = "automated_data_repair"
AUDIT_ACTION = "pending_sql_applied"
AUDIT_TARGET = "pending_sql"
ALREADY_APPLIED = "pending_sql_already_applied"
READ_ONLY_SQLSTATE = "25006"
READ_ONLY_TOKEN = f"钥匙串中的 {KEYCHAIN_SERVICE} 令牌只有只读权限（SQLSTATE 25006），换成可写令牌后重新运行"
_NAME = re.compile(r"^[A-Za-z0-9_./-]+$")
# 托管 Supabase MCP 的 SQL 报错：Failed to run sql query: ERROR:  <SQLSTATE>: <消息>；消息以核对名开头时取出核对名
_SQL_ERROR = re.compile(r"Failed to run sql query: ERROR:\s+([0-9A-Z]{5}): (?:([a-z_]+)(?=:|$|\\n))?", re.M)


def keychain_token(service: str = KEYCHAIN_SERVICE) -> str:
    """从 macOS 钥匙串读取可写访问令牌；缺少时终止并给出创建方法。"""
    result = subprocess.run(["security", "find-generic-password", "-s", service, "-w"], capture_output=True, text=True)
    if result.returncode:
        raise RuntimeError(
            f"钥匙串中没有 {service}：在 https://supabase.com/dashboard/account/tokens 创建访问令牌，再执行 "
            f"`security add-generic-password -U -a costfinder -s {service} -w` 并按提示粘贴令牌")
    return result.stdout.strip()


@asynccontextmanager
async def supabase_session(token: str, url: str = SUPABASE_MCP_URL) -> AsyncIterator[Client]:
    http = create_mcp_http_client(headers={"Authorization": f"Bearer {token}"})
    async with http, Client(streamable_http_client(url, http_client=http)) as client:
        yield client


def guarded_sql(path: Path, root: Path) -> str:
    """文件 SQL 前后加上已执行登记：已登记同一内容即 raise 并不执行；执行成功的同一事务写入登记行。"""
    name = Path(path).relative_to(root).as_posix()
    if not _NAME.match(name):
        raise ValueError(f"invalid_pending_sql_name: {name!r}")
    text = Path(path).read_text(encoding="utf-8")
    # 显式提交会把登记行拆进另一个事务，执行与登记不再同时生效
    if any(isinstance(raw.stmt, ast.TransactionStmt) for raw in parse_sql(text)):
        raise ValueError(f"transaction_control_in_pending_sql: {name}")
    digest = hashlib.sha256(text.encode()).hexdigest()
    match = (f"action = '{AUDIT_ACTION}' and target_type = '{AUDIT_TARGET}' and target_id = '{name}' "
             f"and details->>'sql_sha256' = '{digest}'")
    return (f"do $applied$ begin if exists (select 1 from public.admin_audit_log where {match}) "
            f"then raise exception '{ALREADY_APPLIED}: {name}'; end if; end $applied$;\n"
            f"{text}\n;\n"
            "insert into public.admin_audit_log (admin_id, action, target_type, target_id, details) values "
            f"('{AUDIT_ADMIN}', '{AUDIT_ACTION}', '{AUDIT_TARGET}', '{name}', "
            f"jsonb_build_object('sql_sha256', '{digest}'));\n")


def outcome(is_error: bool, text: str) -> str:
    """applied：执行成功或已登记过同一内容；rejected：文件自身的 SQL 报错（DO 块整体回滚，没有写入）；
    stopped：只读令牌（25006）或不是 SQL 报错的失败（工具、协议），与文件无关。"""
    if not is_error or ALREADY_APPLIED in text:
        return "applied"
    match = _SQL_ERROR.search(text)
    return "rejected" if match and match.group(1) != READ_ONLY_SQLSTATE else "stopped"


def guard_name(text: str) -> str:
    """拒绝原因：DO 块 raise 的核对名（stale_before、write_not_one_row 等），其他 SQL 报错用 SQLSTATE。"""
    match = _SQL_ERROR.search(text)
    return match.group(2) or match.group(1)


async def execute_file(client: Client, path: Path, root: Path) -> tuple[str, str]:
    """执行一个文件；返回 (outcome, 服务返回的文本)。"""
    result = await client.call_tool("execute_sql", {"query": guarded_sql(path, root)})
    text = "".join(part.text for part in result.content)
    return outcome(result.is_error, text), text


def error_path(path: Path) -> Path:
    return Path(path).with_name(Path(path).name + ".error.txt")


async def apply_directory(directory: Path, client: Client, mark: Callable[[Path], object],
                          reject: Callable[[Path, str], object]) -> dict:
    """按文件名顺序执行。文件自身报错的移入 rejected/ 并继续；只读令牌或非 SQL 报错时把错误写入
    <文件>.error.txt 并停止，该文件与其后的文件保持待执行。"""
    directory = Path(directory)
    paths = sorted((path for path, _ in list_pending(directory)), key=lambda p: p.relative_to(directory).as_posix())
    report: dict = {"applied": [], "rejected": [], "stopped": None, "error": None}
    for path in paths:
        result, text = await execute_file(client, path, directory)
        if result == "stopped":
            if READ_ONLY_SQLSTATE in text:
                text = f"{READ_ONLY_TOKEN}\n{text}"
            error_path(path).write_text(text, encoding="utf-8")
            return {**report, "stopped": path, "error": text}
        error_path(path).unlink(missing_ok=True)
        if result == "rejected":
            reject(path, text)
            report["rejected"].append((path, guard_name(text)))
        else:
            mark(path)
            report["applied"].append(path)
    return report


def mark_pulled(path: Path, root: Path, *, base: str = BASE, host: str = HOST) -> None:
    """先在监控主机上登记（已登记则不变），再移动本地文件。"""
    mark_applied_remote(Path(path).relative_to(root).as_posix(), base=base, host=host)
    mark_applied(path)


def reject_pulled(path: Path, error: str, root: Path, *, base: str = BASE, host: str = HOST) -> None:
    """先在监控主机上移入 rejected/（已移入则不变），再移动本地文件；两侧都保存错误原文。"""
    mark_rejected_remote(Path(path).relative_to(root).as_posix(), error, base=base, host=host)
    mark_rejected(path, error)


def install_plist(agents_dir: Path = Path.home() / "Library" / "LaunchAgents") -> Path:
    """把仓库里的 launchd 配置复制到 LaunchAgents，并建立日志目录（launchd 不创建日志的父目录）。"""
    source = PROJECT_ROOT / "config" / "launchd" / f"{LAUNCHD_LABEL}.plist"
    (PROJECT_ROOT / "output" / "launchd").mkdir(parents=True, exist_ok=True)
    target = Path(agents_dir) / source.name
    target.parent.mkdir(parents=True, exist_ok=True)
    shutil.copyfile(source, target)
    return target


def load_agent(path: Path) -> None:
    """重新加载定时任务：已加载时先卸载，再从 path 加载。"""
    domain = f"gui/{os.getuid()}"
    if subprocess.run(["launchctl", "print", f"{domain}/{LAUNCHD_LABEL}"], capture_output=True).returncode == 0:
        subprocess.run(["launchctl", "bootout", f"{domain}/{LAUNCHD_LABEL}"], check=True)
    subprocess.run(["launchctl", "bootstrap", domain, str(path)], check=True)


async def apply_pulled(local_dir: Path, client: Client, *, base: str = BASE, host: str = HOST) -> dict:
    pull_pending(local_dir, base=base, host=host)
    return await apply_directory(local_dir, client, lambda path: mark_pulled(path, local_dir, base=base, host=host),
                                 lambda path, error: reject_pulled(path, error, local_dir, base=base, host=host))
