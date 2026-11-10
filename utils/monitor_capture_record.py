"""监控抓取结果的落盘：抓取证据、页面快照、待处理事件与运行报告。"""
from __future__ import annotations

from collections import Counter
from pathlib import Path
from typing import Any, Iterable
from uuid import uuid4

from utils.monitor_page_visibility import visibility_row
from utils.monitor_price_store import atomic_json, digest, now
from utils.monitor_visibility_evidence import visibility_source


def compare_facts(previous: Iterable[str], current: Iterable[str]) -> dict[str, list[str]]:
    old = set(previous)
    new = set(current)
    return {"added": sorted(new - old), "removed": sorted(old - new)}


def page_event(name: str, page, previous: dict[str, Any], content_digest: str,
               event: dict[str, Any] | None, *, catchup: bool, browser: dict | None) -> dict[str, Any] | None:
    """本次抓取要入队的事件；browser 为 None 表示这个监控组不核验可见性。"""
    def changed(status):
        return {"monitor_name": name, "url": page.url, "status": status,
                "price_diff": compare_facts(previous.get("facts", []), page.facts)}
    if catchup and not previous.get("sync_enqueued"):
        return changed("catchup")
    # Full markdown also detects changed dates, qualification, and removal
    # of image links, which price-line comparison alone cannot establish.
    if previous.get("content_digest") and previous["content_digest"] != content_digest:
        event = event or changed("content_changed")
    return event if browser is None else event or visibility_event(name, page, previous, browser, changed)


def visibility_event(name: str, page, previous: dict[str, Any], browser: dict, changed) -> dict | None:
    if not browser.get("accessible"):
        # Text events remain independent; missing visual proof is retried per page.
        return {"monitor_name": name, "url": page.url, "status": "browser_capture_required", "price_diff": {}}
    if any(browser.get(key) != previous.get(key) for key in ("visual_digest", "context_digest")):
        return changed("visible_content_changed")
    return None


def record_page(name: str, page, rendered: dict | None, previous: dict[str, Any], snapshot_page: dict,
                state_dir: Path, event: dict[str, Any] | None, *, catchup: bool) -> None:
    """保存抓取证据，更新页面快照，并写入待处理事件；rendered 为 None 表示这个监控组不核验可见性。"""
    browser = (rendered or {}).get("browser", {})
    content_digest = digest(page.markdown)
    artifact_id, artifact_path = save_artifact(page, rendered or {}, state_dir)
    snapshot_page.update(content_digest=content_digest, artifact_id=artifact_id)
    if rendered is not None:
        snapshot_page["visibility_source"] = visibility_source(browser)
    if browser.get("accessible"):
        snapshot_page.update(visual_digest=browser.get("visual_digest"), context_digest=browser.get("context_digest"))
    if previous.get("revision"):
        snapshot_page["revision"] = previous["revision"]
    event = page_event(name, page, previous, content_digest, event, catchup=catchup,
                       browser=None if rendered is None else browser)
    if event:
        event_id = digest([name, page.url, previous.get("revision", previous.get("artifact_id")), artifact_id])
        enqueue(state_dir, event, event_id, artifact_id, artifact_path)
        snapshot_page.update(sync_enqueued=True, revision=event_id)
    elif previous.get("sync_enqueued"):
        snapshot_page["sync_enqueued"] = True


def save_artifact(page, rendered: dict, state_dir: Path) -> tuple[str, Path]:
    """保存每次抓取的原始响应与可见性证据，正文未变也保存。"""
    artifact_id = uuid4().hex
    artifact_path = state_dir / "evidence" / (artifact_id + ".json")
    accessible = (rendered.get("browser") or {}).get("accessible")
    atomic_json(artifact_path, {"url": page.url, "captured_at": now(),
                "markdown": page.markdown, "raw_response": page.response,
                "failed_attempts":list(page.failed_attempts),"facts": list(page.facts),
                "rendered":rendered,"browser_fallback":bool(page.failed_attempts and accessible)})
    return artifact_id, artifact_path


def enqueue(state_dir: Path, event: dict[str, Any], event_id: str, artifact_id: str, artifact_path: Path) -> None:
    event_path = state_dir / "events" / (event_id + ".json")
    if not event_path.exists():
        atomic_json(event_path, {**event, "event_id": event_id, "status": "pending",
                    "change_type": event["status"], "created_at": now(),
                    "artifact_id": artifact_id, "artifact_path": str(artifact_path)})


def monitor_report(name: str, baseline: bool, pages: list, events: list[dict[str, Any]], notified: bool,
                   browser_results: dict[str, dict], state_path: Path) -> dict[str, Any]:
    statuses = Counter(page.status for page in pages)
    return {
        "monitor_name": name,
        "baseline": baseline,
        "total_pages": len(pages),
        "same": statuses["same"],
        "new": statuses["new"],
        "changed": statuses["changed"],
        "error": statuses["error"],
        "meaningful_events": len(events),
        "webhook_notified": notified,
        "page_errors": [{"url": page.url, "error": page.error} for page in pages if page.status == "error"],
        "successful_urls": [page.url for page in pages if page.status != "error"],
        "changed_urls": [page.url for page in pages if page.status == "changed"],
        "visibility": [visibility_row(url, row) for url, row in browser_results.items()],
        "state_path": str(state_path),
    }
