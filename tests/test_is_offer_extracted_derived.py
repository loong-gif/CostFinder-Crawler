from __future__ import annotations

import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
MIGRATION = ROOT / "supabase/migrations/20261109090000_derive_is_offer_extracted_on_source_write.sql"
# 渲染写库 SQL 的流水线模块：is_offer_extracted 只由数据库触发器派生，这些模块不写它
WRITERS = ["page_extraction", "membership_sheet_upload", "data_quality_audit",
           "service_linking", "promotion_merge", "offer_stage_sql"]
WRITE = re.compile(r"is_offer_extracted\s*=|[\"']is_offer_extracted[\"']\s*:|is_offer_extracted\)\s*values|"
                   r"is_offer_extracted,\s*(created_at|updated_at|is_ocr_required)")


def test_pipeline_modules_never_write_is_offer_extracted():
    offenders = [name for name in WRITERS if (ROOT / f"utils/{name}.py").exists()
                 and WRITE.search((ROOT / f"utils/{name}.py").read_text())]
    assert offenders == []


def test_source_rows_derive_the_flag_on_every_insert_and_update():
    text = MIGRATION.read_text()
    for table, fk, key in (("clinic_promotions", "promotion_id", "promotion_id"),
                           ("clinic_memberships", "membership_plan_id", "plan_id")):
        assert f"before insert or update on public.{table}" in text
        assert f"m.{fk} = new.{key}" in text
