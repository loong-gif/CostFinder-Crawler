import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from utils.batch_gate_checks import linked_query

queries = {
    "columns": "select column_name,is_nullable,column_default,udt_name,is_generated,generation_expression,is_identity from information_schema.columns where table_schema='public' and table_name='clinic_services' order by ordinal_position",
    "constraints": "select conname,pg_get_constraintdef(oid) as definition from pg_constraint where conrelid='public.clinic_services'::regclass",
    "triggers": "select tgname,pg_get_triggerdef(oid) as definition from pg_trigger where tgrelid='public.clinic_services'::regclass and not tgisinternal",
    "sculptra_categories": "select distinct service_name,service_category from public.clinic_services where service_name::text='Sculptra'",
    "promotion_columns": "select column_name,is_nullable,column_default,udt_name,is_generated,generation_expression,is_identity from information_schema.columns where table_schema='public' and table_name='clinic_promotions' order by ordinal_position",
    "promotion_triggers": "select tgname,pg_get_triggerdef(oid) as definition from pg_trigger where tgrelid='public.clinic_promotions'::regclass and not tgisinternal",
}
results = {}
for name, query in queries.items():
    results[name] = linked_query(query, root=ROOT)
    print(json.dumps({name: results[name]}, ensure_ascii=False))
target = ROOT / 'one-off/outputs/candidates_20261020/service_constraints.json'
target.write_text(json.dumps(results, ensure_ascii=False, indent=2), encoding='utf-8')
