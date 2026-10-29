import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from utils.supabase_access import load_supabase_client

client = load_supabase_client(ROOT)
result = {table: client.fetch_rows(table, '*', filters={key: 'eq.11187'})
          for table, key in [('promo_offer_master', 'id'), ('promo_offer_items', 'offer_id')]}
print(json.dumps(result, ensure_ascii=False, indent=2))
