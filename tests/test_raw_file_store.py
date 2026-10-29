"""Raw Firecrawl tables are file-backed; the production database must never be called for them."""
from __future__ import annotations

import pytest

from utils.firecrawl_scrape_raw_db import save_scrape_response, scrape_request_fingerprint
from utils.firecrawl_search_raw_db import save_search_queries
from utils.raw_file_store import RawFileStore
from utils.supabase_rest import SupabaseRestClient


class _NoNetworkSession:
    def __getattr__(self, name):  # pragma: no cover - failure path
        raise AssertionError(f"raw tables must not hit the network (session.{name})")


@pytest.fixture()
def client(tmp_path, monkeypatch) -> SupabaseRestClient:
    monkeypatch.setenv("COSTFINDER_RAW_DIR", str(tmp_path))
    rest = SupabaseRestClient("https://kdlpkjzcnbkjcvwsvlwn.supabase.co", "key")
    rest.session = _NoNetworkSession()
    return rest


def test_scrape_save_is_upsert_by_fingerprint(client) -> None:
    fp = scrape_request_fingerprint("https://Example.com/specials/")
    first = save_scrape_response(client, fp, "https://example.com/specials", {"data": {"markdown": "v1"}})
    second = save_scrape_response(client, fp, "https://example.com/specials", {"data": {"markdown": "v2"}})
    rows = client.fetch_rows("firecrawl_scrape_raw", "id,source_url,markdown")
    assert first[0]["id"] == second[0]["id"]
    assert len(rows) == 1 and rows[0]["markdown"] == "v2"


def test_search_queries_get_distinct_ids(client) -> None:
    ids = save_search_queries(
        client,
        website="https://example.com",
        domain="example.com",
        entries=[("botox price", [{"url": "https://example.com/a"}]), ("filler price", [])],
    )
    assert len(set(ids.values())) == 2
    rows = client.fetch_rows("firecrawl_search_raw", "id,search_query", filters={"id": f"in.({ids['botox price']})"})
    assert rows == [{"id": ids["botox price"], "search_query": "botox price"}]


def test_filters_order_paging_and_update(client) -> None:
    for index in range(5):
        client.insert_rows(
            "firecrawl_scrape_raw",
            [{"source_url": f"https://e.com/{index}", "success": index % 2 == 0,
              "updated_at": f"2026-10-0{index + 1}T00:00:00+00:00"}],
        )
    ok = client.fetch_rows(
        "firecrawl_scrape_raw", "id", filters={"success": "eq.true", "and": "(updated_at.lt.2026-10-05T00:00:00+00:00)"},
        order="id.asc",
    )
    assert [row["id"] for row in ok] == [1, 3]
    page = client.fetch_rows("firecrawl_scrape_raw", "id", limit=2, offset=1, order="updated_at.desc")
    assert [row["id"] for row in page] == [4, 3]
    client.update_row("firecrawl_scrape_raw", {"id": "eq.2"}, {"success": True})
    assert client.fetch_rows("firecrawl_scrape_raw", "success", filters={"id": "eq.2"}) == [{"success": True}]


def test_unsupported_operator_is_rejected(client) -> None:
    client.insert_rows("firecrawl_scrape_raw", [{"source_url": "https://e.com"}])
    with pytest.raises(ValueError):
        client.fetch_rows("firecrawl_scrape_raw", "id", filters={"source_url": "fts.e"})


def test_store_rejects_non_raw_tables(tmp_path) -> None:
    with pytest.raises(ValueError):
        RawFileStore(tmp_path).fetch_rows("clinic_services")
