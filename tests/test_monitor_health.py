from __future__ import annotations

from monitor_health_support import fake_ego, monitor_services, serve  # noqa: F401
from utils.monitor_health import service_failures


def test_all_services_answer(monitor_services):
    assert service_failures() == []


def test_each_failing_service_is_reported(tmp_path):
    ocr, ocr_url = serve({"/health": (200, b'{"ok": true}')})
    broken, broken_url = serve({"/health": (503, b"{}")})
    failures = service_failures(firecrawl_url=broken_url, ocr_url=broken_url,
                                ego_command=fake_ego(tmp_path / "a", "echo boom >&2; exit 3"))
    assert failures == [{"service": "firecrawl", "error": "HTTP 404"}, {"service": "ocr", "error": "HTTP 503"},
                        {"service": "ego-lite", "error": "exit 3: boom"}]
    unreachable = service_failures(firecrawl_url="http://127.0.0.1:9", ocr_url=ocr_url,
                                   ego_command=fake_ego(tmp_path / "b", "echo ready"))
    broken.shutdown()
    ocr.shutdown()
    assert unreachable == [{"service": "firecrawl", "error": "ConnectionError"},
                           {"service": "ego-lite", "error": "exit 0: ready"}]
