from copy import deepcopy
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from test_monitor_price_writer import plan_for
from utils.monitor_pending_sql import render_pending, settle
from utils.monitor_price_store import atomic_json
from utils.pending_sql import list_pending, mark_applied, pending_dir, read_header, write_pending


def ready_plan(**changes):
    plan = {**plan_for(), "event_id": "evt1", "image_policy": {"version": 1, "depends_on_images": False},
            "prepared_at": datetime.now(timezone.utc).isoformat()}
    plan.update(changes)
    return plan


def test_ready_plan_is_written_as_a_pending_sql_file_with_digest_and_summary(tmp_path):
    receipt = render_pending(ready_plan(), {}, tmp_path)
    assert receipt["status"] == "pending_sql" and receipt["plan_id"] == "evt1"
    path = Path(receipt["sql_path"])
    assert path.parent == pending_dir(tmp_path) and path.name.endswith("_evt1.sql") and path.name[:8].isdigit()
    header = read_header(path)
    assert header[0] == "plan evt1" and header[1] == f"plan sha256 {receipt['plan_sha256']}"
    assert any(line.startswith("source_url:") for line in header) and any(line.startswith("operations:") for line in header)
    assert "do $pipeline$" in path.read_text()


def test_chain_branches_are_rendered_into_one_pending_file(tmp_path):
    from utils.monitor_source_binding import combine_branch_plans

    evidence_path = str(tmp_path / "sync/evt1/evidence.json")
    first = ready_plan(evidence_path=evidence_path)
    second = deepcopy(ready_plan(evidence_path=evidence_path))
    second["source"]["business_id"] = 8
    for group in ("promotions", "offers", "services"):
        for row in second["before"][group]:
            row["business_id"] = 8
    combined = combine_branch_plans("https://clinic.example/specials", [first, second])
    receipt = render_pending(combined, {}, tmp_path)
    assert receipt["status"] == "pending_sql"
    text = Path(receipt["sql_path"]).read_text()
    assert text.count("do $pipeline$") == 2
    assert "business_ids: [7, 8]" in read_header(Path(receipt["sql_path"]))


def test_one_blocked_branch_holds_back_the_whole_page(tmp_path):
    from utils.monitor_source_binding import combine_branch_plans

    evidence_path = str(tmp_path / "sync/evt1/evidence.json")
    stale = ready_plan(evidence_path=evidence_path,
                       prepared_at=(datetime.now(timezone.utc) - timedelta(hours=25)).isoformat())
    combined = combine_branch_plans("https://clinic.example/specials", [stale, ready_plan(evidence_path=evidence_path)])
    assert render_pending(combined, {}, tmp_path)["category"] == "evidence_expired_requires_review"
    assert not pending_dir(tmp_path).exists()


def test_policy_gates_return_review_without_a_file(tmp_path):
    expired = ready_plan(prepared_at=(datetime.now(timezone.utc) - timedelta(hours=25)).isoformat())
    assert render_pending(expired, {}, tmp_path) == {"status": "review", "category": "evidence_expired_requires_review"}
    assert not pending_dir(tmp_path).exists()


def test_old_write_switch_in_the_control_file_no_longer_holds_pending_sql(tmp_path):
    # 写入只生成待执行 SQL，由 MCP 执行把关；服务器上遗留的 write_enabled=false 不再拦截
    atomic_json(tmp_path / "price_sync_control.json", {"write_enabled": False})
    assert render_pending(ready_plan(), {}, tmp_path)["status"] == "pending_sql"


def test_writer_rejection_is_a_review_category_and_leaves_no_file(tmp_path):
    plan = ready_plan()
    plan["operations"][0]["values"] = {"unexpected": 1}
    assert render_pending(plan, {}, tmp_path) == {"status": "review", "category": "invalid_fields"}
    receipt = render_pending(ready_plan(), {"offer_ids": [20]}, tmp_path)
    assert receipt == {"status": "review", "category": "protected_row_or_cascade"}
    assert not pending_dir(tmp_path).exists()


def test_settle_follows_the_file_through_pending_applied_and_missing(tmp_path):
    receipt = render_pending(ready_plan(), {}, tmp_path)
    assert settle(receipt, tmp_path) == "pending_sql"
    path = Path(receipt["sql_path"])
    moved = mark_applied(path)
    assert moved == pending_dir(tmp_path) / "applied" / path.name and moved.is_file() and not path.exists()
    assert settle(receipt, tmp_path) == "completed"
    moved.unlink()
    assert settle(receipt, tmp_path) == "retryable"


def test_listing_covers_flat_files_and_command_output_directories(tmp_path):
    directory = pending_dir(tmp_path)
    flat = write_pending(tmp_path, "plan_a", "0" * 64, {"k": 1}, "select 1;\n")
    nested = directory / "20261102T000000Z" / "apply.sql"
    nested.parent.mkdir(parents=True)
    nested.write_text("-- deactivate-expired apply\nselect 1;\n")
    listed = {path: header for path, header in list_pending(directory)}
    assert set(listed) == {flat, nested}
    assert listed[nested] == ["deactivate-expired apply"] and "k: 1" in listed[flat]
    moved = mark_applied(nested)
    assert moved == directory / "applied" / "20261102T000000Z" / "apply.sql" and moved.is_file()
    assert [p for p, _ in list_pending(directory)] == [flat]


def test_invalid_plan_id_is_rejected(tmp_path):
    with pytest.raises(ValueError):
        write_pending(tmp_path, "../escape", "0" * 64, {}, "select 1;\n")
