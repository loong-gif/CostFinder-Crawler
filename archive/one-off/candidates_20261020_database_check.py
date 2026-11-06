import json
import sys
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from utils.monitor_price_writer_plan import BLANKET_CHECK_SQL
from utils.batch_gate_checks import linked_query

OUT = ROOT / "one-off/outputs/candidates_20261020"
blanket = linked_query(BLANKET_CHECK_SQL, root=ROOT)
columns = linked_query("select table_name,column_name from information_schema.columns where table_schema='public' and table_name in ('master_business_info','firecrawl_search_raw','firecrawl_scrape_raw') order by table_name,ordinal_position", root=ROOT)
brands = linked_query("select business_id,name,address,city,website,google_place_id from master_business_info where name ilike any(array['%simplicity%', '%naked%', '%west dermatology%']) order by business_id", root=ROOT)
(OUT / "database_preflight.json").write_text(json.dumps({"blanket_sql": BLANKET_CHECK_SQL, "blanket_rows": blanket, "columns": columns, "existing_brand_branches": brands}, ensure_ascii=False, indent=2, default=str))
print(json.dumps({"blanket_rows": len(blanket), "schema_columns": len(columns), "brand_branches": len(brands)}))
