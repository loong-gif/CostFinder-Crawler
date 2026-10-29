from __future__ import annotations

import json
import time

import pytest

from scripts import run_data_quality_audit as audit


def _queue(count: int) -> dict:
    return {"entries": [
        {"source_kind": "service", "status": "pending", "source_url": f"https://e{i}.com/p", "business_id": i}
        for i in range(count)
    ]}


@pytest.fixture()
def no_browser(monkeypatch):
    import utils.monitor_rendered_browser as browser
    import utils.offer_extraction_llm as llm

    calls: list[str] = []

    def fake_render(url, directory, **kwargs):
        calls.append(url)
        raise RuntimeError("source_inaccessible_or_redirected")

    monkeypatch.setattr(browser, "render_page", fake_render)
    monkeypatch.setattr(llm, "build_client_from_env", lambda: object())
    return calls


def test_expired_deadline_defers_every_entry_without_rendering(tmp_path, no_browser) -> None:
    queue = _queue(3)
    deferred = audit.recheck_catalog_and_memberships(
        queue, {"memberships": [], "services": []}, tmp_path, deadline=time.monotonic() - 1
    )
    assert deferred == 3 and no_browser == []
    assert all(entry["status"] == "pending" for entry in queue["entries"])
    saved = json.loads((tmp_path / "data_quality" / "recheck_queue.json").read_text())
    assert len(saved["entries"]) == 3


def test_no_deadline_processes_all_entries(tmp_path, no_browser) -> None:
    queue = _queue(2)
    deferred = audit.recheck_catalog_and_memberships(queue, {"memberships": [], "services": []}, tmp_path)
    assert deferred == 0 and len(no_browser) == 2
    assert all(entry["status"] == "review" and entry["attempts"] == 1 for entry in queue["entries"])
