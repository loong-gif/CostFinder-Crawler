"""Rebuild plans using Ego receipts; business data writes stay disabled."""
import json
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from dotenv import load_dotenv

from scripts.run_monitor_price_sync import database_snapshot, prepare_event
from utils.monitor_price_store import atomic_json, manual_protections, now, read_json, run_lock
from utils.monitor_price_writer import normalize_source_url

root = Path('/home/loong/costfinder_monitor/state')
load_dotenv(root.parent / '.env')
with run_lock(root):
    assert read_json(root / 'price_sync_control.json')['write_enabled'] is False
    receipts = read_json(root / 'visual_verifications.json')
    keys = {(normalize_source_url(r['url']), r['artifact_captured_at'], r['artifact_markdown'])
            for r in receipts if r.get('reusable_original_capture')}
    latest = {}
    for path in (root / 'events').glob('*.json'):
        event = read_json(path)
        canonical = normalize_source_url(event['url'])
        if canonical not in latest or event.get('created_at', '') > latest[canonical][1].get('created_at', ''):
            latest[canonical] = (path, event)
    selected = []
    for path, event in latest.values():
        if (event.get('status') not in {'review', 'retryable'}
                or event.get('receipt', {}).get('resume_required')
                or event.get('retry_reason') == 'fresh_scrape_required'):
            continue
        artifact = read_json(Path(event['artifact_path']))
        if artifact and (normalize_source_url(event['url']), artifact.get('captured_at'), artifact.get('markdown')) in keys:
            atomic_json(root / 'nullable_policy_event_backup' / path.name, event)
            event.update(status='retryable', retry_reason='verified_visual_policy_recheck')
            atomic_json(path, event)
            selected.append(path)
    snapshot = database_snapshot()
    protections = manual_protections(snapshot, read_json(root / 'manual_protections.json', {}))
    def prepare(path):
        plan = prepare_event(path, snapshot, protections, root)
        return {'event_id': path.stem, 'url': read_json(path)['url'],
                'status': plan['status'], 'operations': len(plan['operations']),
                'review': plan['review'], 'notes': plan.get('notes', []),
                'prior_visual_receipt': read_json(root / 'sync' / path.stem / 'evidence.json', {}).get('prior_visual_receipt')}
    with ThreadPoolExecutor(max_workers=4) as pool:
        rows = list(pool.map(prepare, selected))
    atomic_json(root / 'nullable_policy_recheck_results.json',
                {'generated_at': now(), 'database_writes': 0, 'sources': rows})
