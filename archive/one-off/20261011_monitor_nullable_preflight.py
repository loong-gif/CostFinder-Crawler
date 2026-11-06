"""Read-only live preflight for the user-authorized nullable monitor policy."""
import sys
import json
import os
from pathlib import Path
from collections import Counter
from datetime import datetime, timezone, timedelta

base = Path('/home/loong/costfinder_monitor')
sys.path.insert(0, str(base))
sys.path.extend(str(p) for p in (base / '.venv/lib').glob('python*/site-packages'))
from dotenv import load_dotenv
load_dotenv(base / '.env')
from scripts.run_monitor_price_sync import database_snapshot, bind_source, snapshot_business_graph, verified_batch_results
from utils.monitor_price_plan import build_plan
from utils.monitor_price_rest_writer import source_graph, _blanket_check
from utils.supabase_rest import SupabaseRestClient, get_supabase_secret_key
from utils.monitor_price_store import read_json, manual_protections, atomic_json, now
from utils.monitor_price_writer_plan import normalize_source_url

state = base / 'state'
snapshot = database_snapshot()
protections = manual_protections(snapshot, read_json(state / 'manual_protections.json', {}))
rows, latest = [], {}
for path in (state / 'events').glob('*.json'):
    event = read_json(path)
    key = normalize_source_url(event['url'])
    if key not in latest or event.get('created_at', '') > latest[key][1].get('created_at', ''):
        latest[key] = (path, event)
for path, event in latest.values():
    if event.get('status') not in {'review', 'retryable', 'planned'}:
        continue
    directory = state / 'sync' / event['event_id']
    evidence = read_json(directory / 'evidence.json')
    old = read_json(directory / 'plan.json', {})
    row = {'url': event['url'], 'event_id': event['event_id'],
           'old_reasons': [x.get('reason') for x in old.get('review', [])]}
    if not evidence:
        rows.append({**row, 'status': 'missing_structured_evidence'})
        continue
    try:
        source = bind_source(event['url'], snapshot)
        graph = source_graph(snapshot_business_graph(snapshot, source['business_id']), source)
        plan = build_plan(source, graph, evidence, protections)
        timestamp = datetime.fromisoformat(old.get('prepared_at', '1970-01-01T00:00:00+00:00').replace('Z', '+00:00'))
        fresh = datetime.now(timezone.utc) - timestamp <= timedelta(hours=24)
        rows.append({**row, 'status': plan['status'], 'fresh': fresh,
                     'operations': len(plan['operations']), 'notes': plan.get('notes', []),
                     'review': plan['review'], 'changes': plan['operations']})
    except Exception as exc:
        rows.append({**row, 'status': 'review', 'category': type(exc).__name__})
client = SupabaseRestClient(os.environ['SUPABASE_URL'], get_supabase_secret_key())
_blanket_check(client)
journals = [read_json(p) for p in (state / 'journals').glob('*.json')]
verified = verified_batch_results(state, snapshot)
result = {'generated_at': now(), 'database_reads_only': True, 'blanket_rows': 0,
          'previously_verified': verified,
          'unfinished_journals': sum(j.get('stage') not in {'completed', 'superseded'} for j in journals),
          'counts': dict(Counter(r['status'] for r in rows)), 'rows': rows}
atomic_json(state / 'nullable_policy_dryrun.json', result)
print(json.dumps(result))
