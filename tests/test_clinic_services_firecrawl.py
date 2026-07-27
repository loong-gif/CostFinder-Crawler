from utils.clinic_services_firecrawl import build_fallback_urls, choose_best_services, extract_rule_based_services, rank_candidate_urls, validate_service
from scripts.run_clinic_services_firecrawl import build_mcp_write_operation, extract_services, is_firecrawl_credit_error, persist_service, process_business
from utils.clinic_services_mcp_sql import render_mcp_operations
from scripts.render_clinic_services_restore_mcp_sql import load_recovery_operations
from utils.direct_public_scrape import html_to_text


def test_rank_candidate_urls_prefers_service_pages_and_keeps_cross_domain_results():
    pages = rank_candidate_urls(
        [
            {"url": "https://example.com/blog/botox-guide"},
            {"url": "https://example.com/specials/botox"},
            {"url": "https://example.com/pricing"},
            {"url": "https://example.com/services/injectables"},
            {"url": "https://other.example/pricing"},
        ],
        domain="example.com",
    )

    assert [page.url for page in pages] == [
        "https://example.com/services/injectables",
        "https://example.com/pricing",
        "https://other.example/pricing",
    ]


def test_build_fallback_urls_uses_only_public_service_path_candidates():
    pages = build_fallback_urls("https://www.example.com/medspa/")

    assert [page.url for page in pages] == [
        "https://www.example.com/medspa",
        "https://www.example.com/medspa/services",
        "https://www.example.com/medspa/pricing",
        "https://www.example.com/medspa/injectables",
    ]
    assert build_fallback_urls("example.com")[0].url == "https://example.com"


def test_is_firecrawl_credit_error_detects_quota_exhaustion():
    assert is_firecrawl_credit_error("Payment Required: Insufficient credits to perform this request")
    assert not is_firecrawl_credit_error("HTTPSConnectionPool timed out")


def test_extract_services_retries_transient_llm_rate_limit(monkeypatch):
    calls = []

    class FakeLlm:
        def create_json_response(self, *_args, **_kwargs):
            calls.append(True)
            if len(calls) == 1:
                raise RuntimeError("429 resource exhausted")
            return {"services": []}

    monkeypatch.setattr("scripts.run_clinic_services_firecrawl.time.sleep", lambda _seconds: None)

    assert extract_services(FakeLlm(), url="https://example.com", markdown="test", max_retries=1) == {"services": []}
    assert len(calls) == 2


def test_extract_services_bounds_model_page_evidence(monkeypatch):
    received = []

    class FakeLlm:
        def create_json_response(self, messages, **_kwargs):
            received.append(messages[-1]["content"])
            return {"services": []}

    monkeypatch.setenv("LLM_MAX_PAGE_CHARS", "1000")
    extract_services(FakeLlm(), url="https://example.com", markdown="x" * 5000)

    assert "x" * 1000 in received[0]
    assert "x" * 1001 not in received[0]


def test_html_to_text_excludes_scripts_and_preserves_service_evidence():
    assert html_to_text("<main>Botox $12 per unit</main><script>ignore()</script>") == "Botox $12 per unit"


def test_rule_fallback_extracts_target_evidence_and_excludes_promotional_price():
    services = extract_rule_based_services(
        "Botox $12 per unit\n"
        "Juvederm $700 per syringe\n"
        "Dysport special $4 per unit\n"
        "Sculptra $800 per vial"
    )

    assert services == [
        {
            "service_category": "Neurotoxins",
            "service_name": "Botox",
            "service_name_raw": "Botox $12 per unit",
            "regular_price": 12.0,
            "unit_type": "unit",
            "service_area": "",
        },
        {
            "service_category": "Fillers & Other Injectables",
            "service_name": "Juvéderm",
            "service_name_raw": "Juvederm $700 per syringe",
            "regular_price": 700.0,
            "unit_type": "syringe",
            "service_area": "",
        },
        {
            "service_category": "Neurotoxins",
            "service_name": "Dysport",
            "service_name_raw": "Dysport special $4 per unit",
            "regular_price": None,
            "unit_type": "others",
            "service_area": "",
        },
        {
            "service_category": "Fillers & Other Injectables",
            "service_name": "Sculptra",
            "service_name_raw": "Sculptra $800 per vial",
            "regular_price": 800.0,
            "unit_type": "vial",
            "service_area": "",
        },
    ]


def test_rule_fallback_does_not_treat_generic_contour_as_restylane():
    assert extract_rule_based_services("A sculpted facial contour with a natural result.") == []


def test_mcp_sql_renderer_casts_enum_fields_and_escapes_evidence():
    sql = render_mcp_operations([
        {"action": "mcp_insert", "payload": {"business_id": 1, "service_name": "Botox", "service_name_raw": "Doctor's Botox", "regular_price": None, "unit_type": "others", "service_category": "Neurotoxin", "service_area": None, "source_url": "https://example.com"}},
    ])

    assert "unit_type::public.unit_type_enum" in sql
    assert "service_category::public.service_category_enum" in sql
    assert "service_area::public.service_area_enum" in sql
    assert "Doctor''s Botox" in sql


def test_mcp_sql_renderer_normalizes_service_area_for_enum_column():
    sql = render_mcp_operations([
        {"action": "mcp_insert", "payload": {"business_id": 1, "service_name": "Botox", "service_name_raw": "Botox", "regular_price": None, "unit_type": "others", "service_category": "Neurotoxin", "service_area": "Denver, Boulder", "source_url": "https://example.com"}},
    ])

    assert '"service_area":null' in sql


def test_restore_loader_normalizes_legacy_service_names(tmp_path):
    audit = {
        "results": [{"writes": [{"mcp_operation": {"action": "mcp_insert", "payload": {
            "business_id": 1, "service_name": "Dermal Fillers", "service_name_raw": "Dermal Fillers",
            "regular_price": None, "unit_type": "others", "service_category": "Filler",
            "service_area": None, "source_url": "https://example.com",
        }}}]}]
    }
    path = tmp_path / "audit.json"
    path.write_text(__import__("json").dumps(audit), encoding="utf-8")

    operations = load_recovery_operations(str(path))

    assert operations[0]["payload"]["service_name"] == "Dermal Filler"


def test_validate_service_keeps_unpriced_public_service():
    item, reason = validate_service(
        {
            "service_category": "Neurotoxins",
            "service_name": "Botox",
            "service_name_raw": "Botox injections are available.",
            "regular_price": None,
            "unit_type": "unit",
            "service_area": "face",
        },
        source_url="https://example.com/services",
        evidence="Botox injections are available.",
    )

    assert reason == "accepted"
    assert item["regular_price"] is None
    assert item["service_category"] == "Neurotoxin"


def test_validate_service_normalizes_unspecified_unit_to_others():
    item, reason = validate_service(
        {
            "service_category": "Fillers & Other Injectables",
            "service_name": "Dermal Fillers",
            "service_name_raw": "Dermal Fillers",
            "regular_price": None,
            "unit_type": "not specified",
            "service_area": "not specified",
        },
        source_url="https://example.com/services",
        evidence="Dermal Fillers",
    )

    assert reason == "accepted"
    assert item["unit_type"] == "others"
    assert item["service_area"] is None


def test_validate_service_rejects_unsupported_price():
    item, reason = validate_service(
        {
            "service_category": "Fillers & Other Injectables",
            "service_name": "Juvederm",
            "service_name_raw": "Juvederm filler.",
            "regular_price": 900,
            "unit_type": "syringe",
            "service_area": "lips",
        },
        source_url="https://example.com/pricing",
        evidence="Juvederm filler.",
    )

    assert item is None
    assert reason == "price_not_supported_by_evidence"


def test_validate_service_rejects_price_found_only_for_another_service():
    item, reason = validate_service(
        {
            "service_category": "Neurotoxins",
            "service_name": "Botox",
            "service_name_raw": "Botox injections are available.",
            "regular_price": 12,
            "unit_type": "unit",
            "service_area": "face",
        },
        source_url="https://example.com/pricing",
        evidence="Botox injections are available. Dysport is $4 per unit.",
    )

    assert item is None
    assert reason == "price_not_supported_by_evidence"


def test_validate_service_rejects_package_unit_type():
    item, reason = validate_service(
        {
            "service_category": "Fillers & Other Injectables",
            "service_name": "Lip filler package",
            "service_name_raw": "Lip filler package $900.",
            "regular_price": 900,
            "unit_type": "package",
            "service_area": "lips",
        },
        source_url="https://example.com/pricing",
        evidence="Lip filler package $900.",
    )

    assert item is None
    assert reason == "invalid_unit_type"


def test_choose_best_services_prefers_priced_evidence_for_duplicate_service():
    accepted, rejected = choose_best_services(
        [
            {
                "service_category": "Neurotoxins",
                "service_name": "Botox",
                "service_name_raw": "Botox is available.",
                "regular_price": None,
                "unit_type": "unit",
                "service_area": "",
            },
            {
                "service_category": "Neurotoxins",
                "service_name": "Botox",
                "service_name_raw": "Botox $12 per unit.",
                "regular_price": 12,
                "unit_type": "unit",
                "service_area": "",
            },
        ],
        source_url="https://example.com/pricing",
        evidence="Botox is available. Botox $12 per unit.",
    )

    assert not rejected
    assert accepted == [
        {
            "service_name": "Botox",
            "service_name_raw": "Botox $12 per unit.",
            "regular_price": 12.0,
            "unit_type": "unit",
            "service_category": "Neurotoxin",
            "service_area": None,
            "source_url": "https://example.com/pricing",
        }
    ]


def test_persist_service_clears_existing_price_with_unpriced_source_evidence():
    class FakeClient:
        def __init__(self):
            self.updated = []

        def fetch_rows(self, *_args, **_kwargs):
            return [{"service_id": 9, "regular_price": 12.0, "source_url": "https://example.com/pricing", "service_name_raw": "Botox $12"}]

        def update_row(self, table, filters, payload):
            self.updated.append((table, filters, payload))
            return [payload]

    client = FakeClient()
    result = persist_service(
        client,
        business_id=1,
        item={
            "service_name": "Botox",
            "service_name_raw": "Botox injections are available.",
            "regular_price": None,
            "unit_type": "unit",
            "service_category": "Neurotoxin",
            "service_area": None,
            "source_url": "https://example.com/services",
        },
        dry_run=False,
    )

    assert result["action"] == "updated"
    assert client.updated[0][2]["regular_price"] is None


def test_build_mcp_write_operation_clears_existing_price_for_unpriced_source_evidence():
    operation = build_mcp_write_operation(
        business_id=1,
        item={
            "service_name": "Botox",
            "service_name_raw": "Botox injections are available.",
            "regular_price": None,
            "unit_type": "unit",
            "service_category": "Neurotoxin",
            "service_area": None,
            "source_url": "https://example.com/services",
        },
        existing={"service_id": 9, "regular_price": 12.0},
    )

    assert operation["action"] == "mcp_update"
    assert operation["match"] == {"service_id": 9}
    assert operation["payload"]["regular_price"] is None


def test_build_mcp_write_operation_clears_placeholder_price_for_unpriced_evidence():
    operation = build_mcp_write_operation(
        business_id=1,
        item={
            "service_name": "Botox",
            "service_name_raw": "Botox injections are available.",
            "regular_price": None,
            "unit_type": "others",
            "service_category": "Neurotoxin",
            "service_area": None,
            "source_url": "https://example.com/services",
        },
        existing={"service_id": 9, "regular_price": 0.01},
    )

    assert operation["payload"]["regular_price"] is None


def test_process_business_persists_direct_public_raw_evidence_during_dry_run(monkeypatch):
    saved = []

    monkeypatch.setattr(
        "scripts.run_clinic_services_firecrawl.scrape_public_page_text",
        lambda _url: ("Botox injections are available.", {"markdown": "Botox injections are available."}),
    )
    monkeypatch.setattr(
        "scripts.run_clinic_services_firecrawl.save_scrape_response",
        lambda *_args, **kwargs: saved.append(kwargs),
    )

    class FakeLlm:
        def create_json_response(self, *_args, **_kwargs):
            return {"services": []}

    result = process_business(
        object(),
        search_client=None,
        scrape_client=None,
        llm=FakeLlm(),
        business={"business_id": 1, "name": "Example", "website": "https://example.com"},
        dry_run=True,
        persist_raw=True,
        direct_public_fallback=True,
    )

    assert result["status"] == "no_accepted_services"
    assert len(saved) == 4
    assert all(item["success"] for item in saved)
