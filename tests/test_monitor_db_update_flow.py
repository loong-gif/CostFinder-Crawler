"""
Unit tests for the Firecrawl monitor -> meaningful-change -> staging update loop.

All external systems (Firecrawl / Apify / Supabase) are stubbed; no real network
calls are made. Covers:
  - page_is_meaningful / check_has_changes / select_checks_to_process (pure logic)
  - process_monitor H1: recrawl error does not advance cursor and breaks at the
    failing check
  - process_monitor M2: meaningful change with unresolvable domain is marked
    "unresolved_domain" and does not advance the cursor
  - sync_crawl_rows_to_staging M3: unchanged rows are batched into a single PATCH

Run:
    python3 -m pytest tests/test_monitor_db_update_flow.py -v
"""
import importlib.util
import sys
import types
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

# utils.observability pulls in opentelemetry, which is not required for these
# tests. Stub it before importing the poll module so the suite runs hermetically.
if "utils.observability" not in sys.modules:
    _obs_stub = types.ModuleType("utils.observability")
    _obs_stub.init_observability = lambda *args, **kwargs: None
    sys.modules["utils.observability"] = _obs_stub


def _load_poll():
    spec = importlib.util.spec_from_file_location(
        "firecrawl_monitor_poll", ROOT / "scripts" / "firecrawl_monitor_poll.py"
    )
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


poll = _load_poll()

from crawler.staging_recrawl import (  # noqa: E402
    MonitorStateRow,
    SyncTarget,
    canonicalize_page_url,
    sync_crawl_rows_to_staging,
)


# --------------------------------------------------------------------------- #
# page_is_meaningful
# --------------------------------------------------------------------------- #
def test_page_is_meaningful_non_changed_status():
    assert poll.page_is_meaningful({"status": "same"}) is False
    assert poll.page_is_meaningful({"status": "removed"}) is False
    assert poll.page_is_meaningful({"status": ""}) is False


def test_page_is_meaningful_missing_judgment_defaults_false():
    assert poll.page_is_meaningful({"status": "changed"}) is False
    assert poll.page_is_meaningful({"status": "new"}) is False


def test_page_is_meaningful_judgment_flag():
    assert poll.page_is_meaningful({"status": "changed", "judgment": {"meaningful": True}}) is True
    assert poll.page_is_meaningful({"status": "new", "judgment": {"meaningful": False}}) is False
    # meaningful key absent inside judgment -> defaults to False
    assert poll.page_is_meaningful({"status": "changed", "judgment": {}}) is False


# --------------------------------------------------------------------------- #
# check_has_changes
# --------------------------------------------------------------------------- #
def test_check_has_changes_missing_or_empty_summary():
    assert poll.check_has_changes({}) is False
    assert poll.check_has_changes({"summary": None}) is False
    assert poll.check_has_changes({"summary": []}) is False
    assert poll.check_has_changes({"summary": {"changed": 0, "new": 0}}) is False


def test_check_has_changes_with_changes():
    assert poll.check_has_changes({"summary": {"changed": 1}}) is True
    assert poll.check_has_changes({"summary": {"new": 2}}) is True


# --------------------------------------------------------------------------- #
# select_checks_to_process
# --------------------------------------------------------------------------- #
def _checks():
    return [
        {"id": "a", "created_at": "2024-01-01"},
        {"id": "b", "created_at": "2024-01-02"},
        {"id": "c", "created_at": "2024-01-03"},
    ]


def _ids(selected):
    return [c["id"] for c in selected]


def test_select_baseline_returns_empty():
    assert poll.select_checks_to_process(_checks(), last_check_id=None, baseline_only=True) == []


def test_select_cursor_hit_returns_newer_oldest_first():
    selected = poll.select_checks_to_process(_checks(), last_check_id="a")
    assert _ids(selected) == ["b", "c"]


def test_select_unknown_cursor_returns_empty():
    selected = poll.select_checks_to_process(_checks(), last_check_id="zzz")
    assert selected == []


def test_select_since_check_returns_newer_oldest_first():
    selected = poll.select_checks_to_process(_checks(), last_check_id=None, since_check="a")
    assert _ids(selected) == ["b", "c"]


def test_select_force_latest_returns_only_latest():
    selected = poll.select_checks_to_process(_checks(), last_check_id="a", force_latest=True)
    assert _ids(selected) == ["c"]


# --------------------------------------------------------------------------- #
# process_monitor helpers
# --------------------------------------------------------------------------- #
class FakeStateStore:
    """In-memory MonitorStateStore stand-in that records every save_state call."""

    def __init__(self, state=None):
        self._state = state
        self.saved = []

    def get_state(self, monitor_id):
        return self._state

    def upsert_mapping(self, monitor_id, domain_name):
        existing = self._state
        self.save_state(
            monitor_id=monitor_id,
            domain_name=domain_name,
            last_check_id=existing.last_check_id if existing else None,
            last_change_at=existing.last_change_at if existing else None,
            last_processed_at=existing.last_processed_at if existing else None,
        )

    def save_state(self, *, monitor_id, domain_name, last_check_id, last_change_at=None, last_processed_at=None):
        self.saved.append({"monitor_id": monitor_id, "last_check_id": last_check_id})
        self._state = MonitorStateRow(
            monitor_id=monitor_id,
            domain_name=domain_name or "",
            last_check_id=last_check_id,
            last_change_at=last_change_at,
            last_processed_at=last_processed_at,
        )

    @property
    def committed_cursors(self):
        return [entry["last_check_id"] for entry in self.saved]


def _completed_check(check_id, created_at, *, changed=1, new=0):
    return {
        "id": check_id,
        "created_at": created_at,
        "status": "completed",
        "summary": {"changed": changed, "new": new},
    }


def _run_process_monitor(monitor, store):
    return poll.process_monitor(
        None,
        monitor,
        store,
        None,
        dry_run=False,
        max_crawl_pages=1,
        crawl_timeout_secs=60,
        since_check=None,
        force_reprocess_latest=False,
    )


def test_process_monitor_recrawl_error_does_not_advance_and_breaks(monkeypatch):
    """H1: a failing recrawl must not advance the cursor, and must break the loop."""
    monitor = {"id": "m1", "name": "shop"}
    store = FakeStateStore(MonitorStateRow(monitor_id="m1", domain_name="shop.com", last_check_id="c0"))

    checks = [
        _completed_check("c0", "2024-01-01"),
        _completed_check("c1", "2024-01-02"),
        _completed_check("c2", "2024-01-03"),
    ]
    monkeypatch.setattr(poll, "list_monitor_checks", lambda fc, mid: checks)
    monkeypatch.setattr(poll, "extract_domains_from_check", lambda fc, mid, cid, **kw: ({"shop.com"}, 1))
    monkeypatch.setattr(
        poll,
        "recrawl_domains",
        lambda domains, **kw: {"shop.com": {"action": "error", "error": "boom"}},
    )

    report = _run_process_monitor(monitor, store)

    # Only the mapping cursor (c0) was ever committed; c1/c2 never advanced.
    assert "c1" not in store.committed_cursors
    assert "c2" not in store.committed_cursors
    # Loop broke at the first (oldest) failing check; c2 was not processed.
    assert len(report["checks_processed"]) == 1
    entry = report["checks_processed"][0]
    assert entry["check_id"] == "c1"
    assert entry["cursor_advanced"] is False
    assert report["status"] == "partial_error"


def test_process_monitor_unresolved_domain_does_not_advance(monkeypatch):
    """M2: a meaningful change with no resolvable domain stays for retry."""
    monitor = {"id": "m2", "name": "no-domain-name"}
    store = FakeStateStore(MonitorStateRow(monitor_id="m2", domain_name="", last_check_id="c0"))

    checks = [
        _completed_check("c0", "2024-01-01"),
        _completed_check("c1", "2024-01-02"),
    ]
    monkeypatch.setattr(poll, "list_monitor_checks", lambda fc, mid: checks)
    # Meaningful pages exist (count=1) but no domain can be parsed out of them.
    monkeypatch.setattr(poll, "extract_domains_from_check", lambda fc, mid, cid, **kw: (set(), 1))

    def _fail_recrawl(*args, **kwargs):
        raise AssertionError("recrawl_domains must not be called for unresolved domains")

    monkeypatch.setattr(poll, "recrawl_domains", _fail_recrawl)

    report = _run_process_monitor(monitor, store)

    assert "c1" not in store.committed_cursors
    entry = report["checks_processed"][0]
    assert entry["action"] == "unresolved_domain"
    assert entry["cursor_advanced"] is False


def test_process_monitor_non_meaningful_summary_change_does_not_recrawl(monkeypatch):
    """M1: summary reports a change but there are no meaningful pages -> no recrawl,
    cursor still advances (the change is non-meaningful and should be ignored)."""
    monitor = {"id": "m1b", "name": "shop"}
    store = FakeStateStore(MonitorStateRow(monitor_id="m1b", domain_name="shop.com", last_check_id="c0"))

    checks = [
        _completed_check("c0", "2024-01-01"),
        _completed_check("c1", "2024-01-02", changed=1),  # summary says changed
    ]
    monkeypatch.setattr(poll, "list_monitor_checks", lambda fc, mid: checks)
    # No meaningful pages at all (count=0) even though summary reported a change.
    monkeypatch.setattr(poll, "extract_domains_from_check", lambda fc, mid, cid, **kw: (set(), 0))

    def _fail_recrawl(*args, **kwargs):
        raise AssertionError("recrawl_domains must not be called for non-meaningful changes")

    monkeypatch.setattr(poll, "recrawl_domains", _fail_recrawl)

    report = _run_process_monitor(monitor, store)

    assert "c1" in store.committed_cursors
    entry = report["checks_processed"][0]
    assert entry["action"] == "no_meaningful_change"
    assert entry["trigger_recrawl"] is False


def test_process_monitor_no_change_advances_cursor(monkeypatch):
    """Guard: a genuine no-change check must still advance the cursor."""
    monitor = {"id": "m3", "name": "shop"}
    store = FakeStateStore(MonitorStateRow(monitor_id="m3", domain_name="shop.com", last_check_id="c0"))

    checks = [
        _completed_check("c0", "2024-01-01"),
        _completed_check("c1", "2024-01-02", changed=0, new=0),
    ]
    monkeypatch.setattr(poll, "list_monitor_checks", lambda fc, mid: checks)
    monkeypatch.setattr(poll, "extract_domains_from_check", lambda fc, mid, cid, **kw: (set(), 0))

    report = _run_process_monitor(monitor, store)

    assert "c1" in store.committed_cursors
    assert report["checks_processed"][0]["action"] == "no_meaningful_change"


# --------------------------------------------------------------------------- #
# sync_crawl_rows_to_staging (M3 batching)
# --------------------------------------------------------------------------- #
class FakeSupabaseClient:
    def __init__(self, existing_rows):
        self._existing_rows = existing_rows
        self.update_calls = []
        self.insert_calls = []

    def fetch_rows(self, table, select, *, filters=None, limit=None, offset=None, order=None):
        if offset and offset > 0:
            return []
        return [dict(row) for row in self._existing_rows]

    def update_row(self, table, filters, payload):
        self.update_calls.append((dict(filters), dict(payload)))
        return []

    def insert_rows(self, table, rows):
        self.insert_calls.append([dict(r) for r in rows])
        return rows

    def upsert_rows(self, *args, **kwargs):  # pragma: no cover - must not be hit
        raise AssertionError("upsert_rows should not be used for partial-column updates")


def _crawl_row(url, content, *, ts, name="n"):
    return {
        "crawl_timestamp": ts,
        "subpage_url": url,
        "page_content": content,
        "domain_name": "ex.com",
        "processed_status": False,
        "name": name,
    }


def _existing_row(pid, url, content, *, name="n"):
    return {
        "promo_website_id": pid,
        "domain_name": "ex.com",
        "subpage_url": url,
        "page_content": content,
        "crawl_timestamp": "2023-01-01T00:00:00+00:00",
        "processed_status": True,
        "name": name,
    }


def _build_sync_inputs():
    ts = "2024-06-01T00:00:00+00:00"
    existing = [
        _existing_row(1, "https://ex.com/a", "A"),
        _existing_row(2, "https://ex.com/b", "B"),
        _existing_row(3, "https://ex.com/c", "C"),
    ]
    crawl_rows = [
        _crawl_row("https://ex.com/a", "A", ts=ts),       # unchanged
        _crawl_row("https://ex.com/b", "B", ts=ts),       # unchanged
        _crawl_row("https://ex.com/c", "C2", ts=ts),      # content changed
        _crawl_row("https://ex.com/d", "D", ts=ts),       # new -> insert
    ]
    target = SyncTarget(
        domain_name="ex.com",
        website_url="https://ex.com",
        name="n",
        master_id=None,
        business_id=None,
    )
    return existing, crawl_rows, target, ts


def test_sync_skips_unchanged_and_updates_last_updated_at():
    existing, crawl_rows, target, ts = _build_sync_inputs()
    client = FakeSupabaseClient(existing)

    report = sync_crawl_rows_to_staging(client, target, crawl_rows, dry_run=False)

    # Report fields preserved and semantically correct.
    assert report["existing_rows"] == 3
    assert report["crawl_rows"] == 4
    assert report["matched_rows"] == 3
    assert report["content_changed_rows"] == 1
    assert report["timestamp_only_rows"] == 2  # unchanged rows, now skipped
    assert report["insert_rows"] == 1
    assert report["updated_rows"] == 1
    assert report["inserted_rows"] == 1

    # Unchanged rows issue NO write; only the changed row is PATCHed.
    assert len(client.update_calls) == 1
    assert len(client.insert_calls) == 1

    # Changed row: refreshes last_updated_at, resets processed_status, leaves
    # crawl_timestamp untouched (not present in the PATCH payload).
    changed_filter, changed_payload = client.update_calls[0]
    assert changed_filter["promo_website_id"] == "eq.3"
    assert changed_payload["processed_status"] is False
    assert changed_payload["page_content"] == "C2"
    assert "last_updated_at" in changed_payload
    assert "crawl_timestamp" not in changed_payload

    # Insert: brand-new row carries both crawl_timestamp (first crawl) and last_updated_at.
    inserted = client.insert_calls[0][0]
    assert inserted["subpage_url"] == "https://ex.com/d"
    assert inserted["crawl_timestamp"] == ts
    assert "last_updated_at" in inserted


def test_sync_dry_run_issues_no_writes():
    existing, crawl_rows, target, _ = _build_sync_inputs()
    client = FakeSupabaseClient(existing)

    report = sync_crawl_rows_to_staging(client, target, crawl_rows, dry_run=True)

    assert client.update_calls == []
    assert client.insert_calls == []
    assert report["updated_rows"] == 1
    assert report["inserted_rows"] == 1
    assert report["content_changed_rows"] == 1
    assert report["timestamp_only_rows"] == 2


class FakeLlmClient:
    def __init__(self, model="test-model"):
        self.model = model


def test_process_monitor_baseline_initializes_cursor(monkeypatch):
    monitor = {"id": "m-baseline", "name": "shop.com"}
    store = FakeStateStore(None)
    checks = [
        _completed_check("c1", "2024-01-02"),
        _completed_check("c0", "2024-01-01"),
    ]
    monkeypatch.setattr(poll, "list_monitor_checks", lambda fc, mid: checks)

    report = poll.process_monitor(
        None,
        monitor,
        store,
        None,
        dry_run=False,
        max_crawl_pages=1,
        crawl_timeout_secs=60,
        since_check=None,
        force_reprocess_latest=False,
    )

    assert report["status"] == "baseline_initialized"
    assert report["baseline_check_id"] == "c1"
    assert store.committed_cursors[-1] == "c1"


def test_process_monitor_dry_run_does_not_write_cursor(monkeypatch):
    monitor = {"id": "m-dry", "name": "shop.com"}
    store = FakeStateStore(MonitorStateRow(monitor_id="m-dry", domain_name="shop.com", last_check_id="c0"))
    checks = [
        _completed_check("c0", "2024-01-01"),
        _completed_check("c1", "2024-01-02", changed=0, new=0),
    ]
    monkeypatch.setattr(poll, "list_monitor_checks", lambda fc, mid: checks)
    monkeypatch.setattr(poll, "extract_domains_from_check", lambda fc, mid, cid, **kw: (set(), 0))

    report = poll.process_monitor(
        None,
        monitor,
        store,
        None,
        dry_run=True,
        max_crawl_pages=1,
        crawl_timeout_secs=60,
        since_check=None,
        force_reprocess_latest=False,
    )

    assert "c1" not in store.committed_cursors
    assert report["checks_processed"][0]["action"] == "no_meaningful_change"


def test_process_monitor_dry_run_still_upserts_domain_mapping(monkeypatch):
    """dry-run 不推进游标，但 upsert_mapping 仍会持久化 domain 绑定（既有行为）。"""
    monitor = {"id": "m-dry-map", "name": "shop.com"}
    store = FakeStateStore(MonitorStateRow(monitor_id="m-dry-map", domain_name="", last_check_id="c0"))
    checks = [
        _completed_check("c0", "2024-01-01"),
        _completed_check("c1", "2024-01-02", changed=0, new=0),
    ]
    monkeypatch.setattr(poll, "list_monitor_checks", lambda fc, mid: checks)
    monkeypatch.setattr(poll, "infer_domain_from_monitor", lambda monitor, store: "shop.com")
    monkeypatch.setattr(poll, "extract_domains_from_check", lambda fc, mid, cid, **kw: (set(), 0))

    poll.process_monitor(
        None,
        monitor,
        store,
        None,
        dry_run=True,
        max_crawl_pages=1,
        crawl_timeout_secs=60,
        since_check=None,
        force_reprocess_latest=False,
    )

    assert store.committed_cursors == ["c0"]
    assert store.get_state("m-dry-map").domain_name == "shop.com"


def test_process_monitor_change_driven_skips_apify(monkeypatch):
    monitor = {"id": "m-cd", "name": "shop.com"}
    store = FakeStateStore(MonitorStateRow(monitor_id="m-cd", domain_name="shop.com", last_check_id="c0"))
    checks = [
        _completed_check("c0", "2024-01-01"),
        _completed_check("c1", "2024-01-02"),
    ]
    monkeypatch.setattr(poll, "list_monitor_checks", lambda fc, mid: checks)
    monkeypatch.setattr(
        poll,
        "fetch_meaningful_pages",
        lambda fc, mid, cid, **kw: (
            [{"url": "https://shop.com/specials", "status": "changed", "judgment": {"meaningful": True}}],
            1,
        ),
    )

    def _fail_recrawl(*args, **kwargs):
        raise AssertionError("recrawl_domains must not run when change-driven covers all pages")

    monkeypatch.setattr(poll, "recrawl_domains", _fail_recrawl)
    monkeypatch.setattr(
        "utils.change_driven_extractor.extract_and_upsert_check_pages",
        lambda pages, llm, db, domain, **kw: {
            "needs_apify_fallback": False,
            "pages_with_diff": 1,
            "pages_without_diff": 0,
            "total_offers_extracted": 1,
            "total_updated": 1,
            "total_inserted": 0,
            "total_ended": 0,
            "total_auto_apply_events": 1,
            "total_review_events": 0,
            "candidates_unavailable": False,
            "page_results": [],
        },
    )

    report = poll.process_monitor(
        None,
        monitor,
        store,
        object(),
        dry_run=False,
        max_crawl_pages=1,
        crawl_timeout_secs=60,
        since_check=None,
        force_reprocess_latest=False,
        llm_client=FakeLlmClient(),
        skip_apify_on_success=True,
    )

    assert report["checks_processed"][0]["trigger_recrawl"] is True
    assert report["recrawls"][0]["action"] == "skipped_change_driven"
    assert "c1" in store.committed_cursors


def test_process_monitor_change_driven_error_still_recrawls(monkeypatch):
    monitor = {"id": "m-cd-err", "name": "shop.com"}
    store = FakeStateStore(
        MonitorStateRow(monitor_id="m-cd-err", domain_name="shop.com", last_check_id="c0")
    )
    checks = [
        _completed_check("c0", "2024-01-01"),
        _completed_check("c1", "2024-01-02"),
    ]
    monkeypatch.setattr(poll, "list_monitor_checks", lambda fc, mid: checks)
    monkeypatch.setattr(
        poll,
        "fetch_meaningful_pages",
        lambda fc, mid, cid, **kw: (
            [{"url": "https://shop.com/specials", "status": "changed", "judgment": {"meaningful": True}}],
            1,
        ),
    )
    monkeypatch.setattr(
        "utils.change_driven_extractor.extract_and_upsert_check_pages",
        lambda *args, **kwargs: (_ for _ in ()).throw(RuntimeError("llm pipeline boom")),
    )
    recrawl_calls = []

    def _recrawl(domains, **kw):
        recrawl_calls.append(list(domains))
        return {"shop.com": {"action": "synced", "updated_rows": 1}}

    monkeypatch.setattr(poll, "recrawl_domains", _recrawl)

    report = poll.process_monitor(
        None,
        monitor,
        store,
        object(),
        dry_run=False,
        max_crawl_pages=1,
        crawl_timeout_secs=60,
        since_check=None,
        force_reprocess_latest=False,
        llm_client=FakeLlmClient(),
        skip_apify_on_success=True,
    )

    assert recrawl_calls == [["shop.com"]]
    assert report["checks_processed"][0].get("change_driven_error") == "llm pipeline boom"
    assert report["recrawls"][0]["action"] == "synced"
    assert "c1" in store.committed_cursors
