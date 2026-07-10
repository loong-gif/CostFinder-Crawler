from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

SCRIPT = PROJECT_ROOT / "scripts" / "build_offer_evidence_segments.py"
spec = importlib.util.spec_from_file_location("build_offer_evidence_segments", SCRIPT)
mod = importlib.util.module_from_spec(spec)
assert spec.loader is not None
spec.loader.exec_module(mod)


def test_build_page_result_returns_only_offer_segments_by_default():
    row = {
        "promo_website_id": 1,
        "subpage_url": "https://example.com/pricing",
        "domain_name": "example.com",
        "name": "Example Med Spa",
        "page_content": "[SEGMENT 0] About us\n[SEGMENT 1] Injectables Botox $11 Per Unit",
    }
    result = mod.build_page_result(row, include_all_segments=False)
    assert result["summary"]["segment_count"] == 2
    assert result["summary"]["offer_signal_count"] == 1
    assert len(result["segments"]) == 1
    assert result["segments"][0]["segment_index"] == 1


def test_write_artifacts_creates_json_and_summary_csv(tmp_path):
    row = {
        "promo_website_id": 1,
        "subpage_url": "https://example.com/pricing",
        "domain_name": "example.com",
        "name": "Example Med Spa",
        "page_content": "[SEGMENT 1] Injectables Botox $11 Per Unit",
    }
    page = mod.build_page_result(row, include_all_segments=True)
    paths = mod.write_artifacts(tmp_path, [page])
    json_path = Path(paths["json_path"])
    csv_path = Path(paths["csv_path"])
    assert json_path.exists()
    assert csv_path.exists()
    assert "_%f" not in json_path.name
    assert json_path.name.startswith("offer_evidence_segments_")
    assert json_path.read_text(encoding="utf-8").count("Botox") >= 1
