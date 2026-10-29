"""Apply two already visually verified offers through the existing REST writer."""
from pathlib import Path
from dotenv import load_dotenv

from scripts.run_monitor_price_sync import (database_snapshot, bind_source, snapshot_business_graph,
                                          target_client, verified_batch_results)
from utils.monitor_price_plan import build_plan
from utils.monitor_price_rest_writer import apply_plan, source_graph, _blanket_check
from utils.monitor_price_store import atomic_json, manual_protections, now, read_json, run_lock

root = Path('/home/loong/costfinder_monitor/state')
load_dotenv(root.parent / '.env')
event_ids = ['4d91706089182d87b524663474b0eec8048b918f8d75c7dcb9fee05089c22ee0',
             '57601f854e9962d83f82af408bdbb1ef3009af8d202696389adf6c8f0d9c71ff']
with run_lock(root):
    assert read_json(root / 'price_sync_control.json')['write_enabled'] is False
    receipts = []
    client = target_client()
    for event_id in event_ids:
        event_path = root / 'events' / (event_id + '.json')
        event = read_json(event_path)
        evidence = read_json(root / 'sync' / event_id / 'evidence.json')
        assert evidence.get('prior_visual_receipt'), 'prior_ego_evidence_required'
        snapshot = database_snapshot()
        protections = manual_protections(snapshot, read_json(root / 'manual_protections.json', {}))
        source = bind_source(event['url'], snapshot)
        graph = source_graph(snapshot_business_graph(snapshot, source['business_id']), source)
        plan = build_plan(source, graph, evidence, protections)
        plan.update(event_id=event_id, prepared_at=now(),
                    evidence_path=str(root / 'sync' / event_id / 'evidence.json'))
        assert plan['status'] == 'ready', 'verified_source_not_ready'
        directory = root / 'nullable_policy_apply' / event_id
        atomic_json(directory / 'plan.json', plan)
        atomic_json(directory / 'evidence.json', evidence)
        receipt = apply_plan(plan, client, root / 'journals', protections)
        atomic_json(directory / 'receipt.json', receipt)
        event.update(status=receipt['status'], receipt=receipt)
        atomic_json(event_path, event)
        receipts.append({'url': source['url'], **receipt})
    _blanket_check(client)
    snapshot = database_snapshot()
    result = {'generated_at': now(), 'receipts': receipts, 'blanket_rows': 0,
              'previously_verified': verified_batch_results(root, snapshot)}
    atomic_json(root / 'nullable_policy_apply_results.json', result)
    assert all(r['status'] == 'completed' for r in receipts), 'nullable_live_acceptance_incomplete'
