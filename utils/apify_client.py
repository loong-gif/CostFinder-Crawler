"""
Shared helpers for invoking Apify actors and reading dataset items.
"""
from __future__ import annotations

import json
import subprocess
from tempfile import NamedTemporaryFile
from typing import Any, Dict, List, Sequence


def run_cli_json(command: Sequence[str]) -> Dict[str, Any]:
    result = subprocess.run(command, capture_output=True, text=True, check=False)
    if result.returncode != 0:
        raise RuntimeError((result.stderr or result.stdout or "").strip() or "命令执行失败")
    try:
        payload = json.loads(result.stdout)
    except json.JSONDecodeError as exc:
        raise RuntimeError(f"命令输出不是合法 JSON: {exc}") from exc
    if not isinstance(payload, dict):
        raise RuntimeError("命令输出格式异常，预期为 JSON 对象")
    return payload


def run_actor(actor_id: str, actor_input: Dict[str, Any], *, timeout_secs: int) -> Dict[str, Any]:
    with NamedTemporaryFile("w", suffix=".json", delete=True, encoding="utf-8") as handle:
        json.dump(actor_input, handle, ensure_ascii=False)
        handle.flush()
        return run_cli_json(
            [
                "apify",
                "actors",
                "call",
                actor_id,
                "--input-file",
                handle.name,
                "--silent",
                "--json",
                "--timeout",
                str(timeout_secs),
            ]
        )


def fetch_dataset_items(dataset_id: str) -> List[Dict[str, Any]]:
    result = subprocess.run(
        ["apify", "datasets", "get-items", dataset_id, "--format", "json"],
        capture_output=True,
        text=True,
        check=False,
    )
    if result.returncode != 0:
        raise RuntimeError((result.stderr or result.stdout or "").strip() or "拉取 dataset 失败")
    try:
        payload = json.loads(result.stdout)
    except json.JSONDecodeError as exc:
        raise RuntimeError(f"dataset 输出不是合法 JSON: {exc}") from exc
    if not isinstance(payload, list):
        raise RuntimeError("dataset 输出格式异常，预期为 JSON 数组")
    return [item for item in payload if isinstance(item, dict)]


def extract_default_dataset_id(run_payload: Dict[str, Any]) -> str:
    if not isinstance(run_payload, dict):
        return ""

    data_payload = run_payload.get("data") if isinstance(run_payload.get("data"), dict) else {}
    dataset_payload = data_payload.get("defaultDataset") if isinstance(data_payload.get("defaultDataset"), dict) else {}
    candidates = [
        run_payload.get("defaultDatasetId"),
        data_payload.get("defaultDatasetId"),
        dataset_payload.get("id"),
    ]
    for candidate in candidates:
        if isinstance(candidate, str) and candidate.strip():
            return candidate.strip()
    return ""
