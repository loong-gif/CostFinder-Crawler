#!/usr/bin/env python3
"""Evaluate frozen screenshots locally. No monitor, REST client or writer calls."""
from __future__ import annotations

import argparse
from collections import defaultdict
from pathlib import Path
import shutil
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from dotenv import load_dotenv
from PIL import Image

from utils.monitor_image_evaluation import expected_offers, match_score, score_job, summarize, validate_truth
from utils.monitor_image_evidence import tile_ranges
from utils.monitor_price_extract import extract_evidence
from utils.monitor_price_plan import build_plan
from utils.monitor_price_store import atomic_json, now, read_json
from utils.offer_extraction_llm import build_client_from_env


def prepare_browser(rows, directory):
    """Only manifest identity and original pixels enter extraction, never truth."""
    paths = list(dict.fromkeys(path for row in rows for path in row['screenshot_paths']))
    tiles = {'a': [], 'b': []}
    for index, filename in enumerate(paths):
        with Image.open(filename) as source:
            for round_name, shifted in (('a', False), ('b', True)):
                for part, (y, height) in enumerate(tile_ranges(source.height, shifted=shifted)):
                    target = directory / 'tiles' / f'{round_name}_{index:03d}_{part:03d}.png'
                    target.parent.mkdir(parents=True, exist_ok=True)
                    source.convert('RGB').crop((0, y, source.width, y + height)).save(target)
                    tiles[round_name].append({'path': str(target.resolve()), 'source_path': filename,
                        'region': {'x': 0, 'y': y, 'width': source.width, 'height': height}})
    # Historical review screenshots have no machine-verifiable full coverage receipt.
    # Extract visible independent facts but do not invent whole-source removal proof.
    return {'url': rows[0]['url'], 'captured_at': rows[0]['observed_at'], 'accessible': bool(paths),
            'complete': False, 'text': '', 'image_review_required': True, 'image_tiles': tiles,
            'image_write_enabled': True, 'image_capture_mode': 'archived_screenshots',
            'failures': ['archived_coverage_unproven'], 'image_seconds_remaining': 900}


def safety_graph(source):
    """Explicit synthetic sibling tests deletion safety, not live DB accuracy."""
    business_id, url = source['business_id'], source['url']
    return {'promotions': [{'promotion_id': 1, 'business_id': business_id, 'source_url': url,
        'promotion_title': 'Previous promotion', 'campaign_start_date': None, 'campaign_end_date': None,
        'promotion_content': ['Previous independent offer'], 'is_active': True, 'is_offer_extracted': True}],
        'offers': [{'id': 1, 'business_id': business_id, 'promotion_id': 1, 'membership_plan_id': None,
            'offer_raw_text': 'Previous independent offer', 'price_model': 'total', 'regular_price': None,
            'discount_price': 100, 'discount_percent': None, 'discount_amount': None,
            'is_package': False, 'is_active': True, 'conditions': [],
            'is_membership_required': False, 'is_new_customer_required': False}],
        'items': [{'offer_item_id': 1, 'offer_id': 1, 'service_id': None,
            'item_name': 'Previous independent offer', 'delivered_units': None, 'unit_price': 100}],
        'services': [], 'terms': []}


def plans_for(rows, evidence):
    merchant = rows[0]['merchant']
    business_id = int(merchant.removeprefix('business_')) if merchant.startswith('business_') else 1
    source = {'business_id': business_id, 'url': rows[0]['url']}
    graph = {key: [] for key in ('promotions', 'offers', 'items', 'services', 'terms')}
    # Existing prices/IDs are deliberately not evaluated from an obsolete DB snapshot.
    return build_plan(source, graph, evidence, {}), build_plan(source, safety_graph(source), evidence, {})


def safe_error(exc):
    response = getattr(exc, 'response', None)
    status = getattr(response, 'status_code', None)
    return {'type': type(exc).__name__, 'http_status': status,
            'reason': 'model_request_failed' if response is not None else type(exc).__name__}


def run(manifest, truth, output, split, run_kind, client, matching=None, service_names=None):
    if truth.get('freeze_status') != 'frozen':
        raise ValueError('truth_not_frozen')
    validation = validate_truth(manifest, truth)
    if not validation['valid']:
        raise ValueError('truth_validation_failed')
    groups = defaultdict(list)
    for row in manifest['samples']:
        if row['split'] == split:
            groups[row['job_id']].append(row)
    output = Path(output)
    output.mkdir(parents=True, exist_ok=True)
    config = {'split': split, 'run_kind': run_kind, 'model': client.model,
        'client_class': type(client).__name__, 'started_at': now(), 'jobs': len(groups),
        'database_writes': 0, 'production_baseline_changes': 0, 'production_code_changes': 0,
        'input_policy': 'Manifest screenshot paths only; no truth, locators or database inputs to model.',
        'coverage_policy': 'Archived full-page coverage unproven. Removal safety uses a synthetic sibling.',
        'field_denominator': 'Only explicit scorable truth fields on matched offers; misses counted in recall.'}
    existing = read_json(output / 'run_config.json')
    if existing and any(existing.get(k) != config[k] for k in ('split', 'run_kind', 'model', 'client_class')):
        raise ValueError('run_configuration_changed_use_new_directory')
    if not existing:
        atomic_json(output / 'run_config.json', config)
        for name in ('utils/monitor_image_evaluation.py', 'utils/monitor_image_evidence.py',
                     'utils/monitor_price_extract.py', 'utils/monitor_price_plan.py',
                     'utils/monitor_price_plan_campaign.py', 'utils/monitor_price_plan_common.py',
                     'utils/monitor_price_plan_expected.py', 'utils/monitor_price_plan_identity.py',
                     'utils/monitor_price_plan_offer.py', 'utils/monitor_price_plan_variant.py',
                     'utils/offer_purchase_semantics.py', 'utils/offer_evidence_validator.py',
                     'utils/service_price_guard.py', 'utils/offer_extraction_llm.py',
                     'schema/offer_extraction_schema.json', 'scripts/run_monitor_image_evaluation.py'):
            target = output / 'code_snapshot' / name
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(ROOT / name, target)
        atomic_json(output / 'input_manifest.json', {'samples': [
            {k: row[k] for k in ('sample_id', 'job_id', 'merchant', 'split', 'url', 'observed_at', 'screenshot_paths')}
            for rows in groups.values() for row in rows]})
        atomic_json(output / 'truth_frozen.json', truth)
    else:
        if read_json(output / 'truth_frozen.json') != truth:
            raise ValueError('frozen_truth_changed_use_new_directory')
        for saved in (output / 'code_snapshot').rglob('*'):
            if saved.is_file() and saved.read_bytes() != (ROOT / saved.relative_to(output / 'code_snapshot')).read_bytes():
                raise ValueError('code_changed_use_new_run_directory')
    results, failed = [], []
    for job_id, rows in groups.items():
        job_directory = output / job_id
        job_directory.mkdir(exist_ok=True)
        receipt = read_json(job_directory / 'result.json')
        if receipt is None:
            attempts = list(job_directory.glob('attempt_*'))
            directory = job_directory / f'attempt_{len(attempts) + 1:02d}'
            directory.mkdir()
            try:
                browser = prepare_browser(rows, directory)
                atomic_json(directory / 'browser.json', browser)
                evidence = extract_evidence({'url': rows[0]['url']}, browser, directory, client, service_names)
                # extract_evidence catches image transport failures; never score them as zero recall.
                dual = read_json(directory / 'image_dual_read.json')
                if dual is None:
                    raise RuntimeError('image_inference_incomplete')
                evidence['observed_date'] = rows[0]['observed_at'][:10]
                plan, removal_probe = plans_for(rows, evidence)
                atomic_json(directory / 'plan.json', plan)
                atomic_json(directory / 'removal_safety_probe.json', removal_probe)
                samples = [truth['samples'][row['sample_id']] for row in rows]
                job_overrides = matching.get(job_id) if matching is not None else None
                raw_offers = dual['reads']['a']['offers']
                score = score_job(samples, raw_offers, removal_probe, job_overrides)
                released = [op['values'] for op in plan['operations'] if op['table'] == 'promo_offer_master'
                            and op['values'].get('is_active') is True]
                review_cases = [e for sample in samples for e in expected_offers(sample) if e['requires_review']]
                violations = [e['expected_id'] for e in review_cases
                              if any(match_score(e, offer) >= .35 for offer in released)]
                score['counts'].update(review_required_cases=len(review_cases), review_required_released=len(violations))
                receipt = {'job_id': job_id, 'split': split, 'completed_at': now(),
                    'inference_directory': str(directory.resolve()),
                    'raw': score, 'dual': score_job(samples, dual['accepted'], removal_probe),
                    'plan_status': plan['status'], 'plan_review': plan['review'],
                    'released_offer_count': len(released), 'review_release_violations': violations,
                    'database_writes': 0, 'production_baseline_changes': 0}
                atomic_json(job_directory / 'result.json', receipt)
            except Exception as exc:
                failure = {'job_id': job_id, 'failed_at': now(), **safe_error(exc)}
                atomic_json(directory / 'failure.json', failure)
                failed.append(failure)
                # Auth, quota, or inference transport failure needs investigation before more calls.
                if failure['http_status'] in (401, 402, 403, 429) or isinstance(exc, RuntimeError):
                    break
                continue
        results.append(receipt)
        atomic_json(output / 'progress.json', {'completed_jobs': len(results), 'total_jobs': len(groups),
            'last_job': job_id, 'updated_at': now(), 'failures': failed})
        print(f'{split}: {len(results)}/{len(groups)} {job_id}', flush=True)
    summary = {'split': split, 'run_kind': run_kind, 'completed_jobs': len(results),
        'total_jobs': len(groups), 'inference_complete': len(results) == len(groups) and not failed,
        'raw': summarize([r['raw'] for r in results]), 'dual': summarize([r['dual'] for r in results]),
        'failures': failed, 'finished_at': now(), 'database_writes': 0, 'production_baseline_changes': 0}
    atomic_json(output / 'summary.json', summary)
    return summary


def rescore(output, matching):
    """Review persisted first outputs without further model calls or overwrites."""
    output = Path(output)
    truth = read_json(output / 'truth_frozen.json')
    manifest = read_json(output / 'input_manifest.json')
    groups = defaultdict(list)
    for row in manifest['samples']:
        groups[row['job_id']].append(truth['samples'][row['sample_id']])
    scores, results = [], []
    for job_id, samples in groups.items():
        receipt = read_json(output / job_id / 'result.json')
        if receipt is None:
            continue
        directory = Path(receipt['inference_directory'])
        dual = read_json(directory / 'image_dual_read.json')
        probe = read_json(directory / 'removal_safety_probe.json')
        score = score_job(samples, dual['reads']['a']['offers'], probe, matching.get(job_id))
        scores.append(score)
        results.append({'job_id': job_id, **score})
    atomic_json(output / 'adjudicated_results.json', results)
    summary = {'completed_jobs': len(results), 'total_jobs': len(groups), 'raw': summarize(scores),
               'database_writes': 0, 'production_baseline_changes': 0, 'scored_at': now()}
    atomic_json(output / 'adjudicated_summary.json', summary)
    return summary


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--manifest', type=Path)
    parser.add_argument('--truth', type=Path)
    parser.add_argument('--output', type=Path, required=True)
    parser.add_argument('--split', choices=('development', 'held_out'))
    parser.add_argument('--run-kind', choices=('first', 'retest'), default='first')
    parser.add_argument('--development-receipt', type=Path)
    parser.add_argument('--matching', type=Path)
    parser.add_argument('--score-only', action='store_true')
    parser.add_argument('--service-names', type=Path,
                        help='JSON list of clinic_service_name_enum values (the monitor snapshot service_names)')
    args = parser.parse_args()
    if args.score_only:
        if not args.matching:
            parser.error('--matching required for score-only')
        rescore(args.output, read_json(args.matching))
        return 0
    if not args.manifest or not args.truth or not args.split:
        parser.error('--manifest, --truth and --split required for inference')
    if not args.service_names:
        parser.error('--service-names required for inference')
    if args.split == 'held_out' and args.run_kind == 'first':
        receipt = read_json(args.development_receipt) if args.development_receipt else None
        if not receipt or receipt.get('split') != 'development' or not receipt.get('inference_complete'):
            parser.error('complete development evaluation required before held_out first run')
    load_dotenv(ROOT / '.env')
    client = build_client_from_env()
    if client is None or not hasattr(client, 'create_json_response_with_images'):
        parser.error('configured image model client required')
    service_names = read_json(args.service_names)
    result = run(read_json(args.manifest), read_json(args.truth), args.output, args.split,
                 args.run_kind, client, read_json(args.matching) if args.matching else None, service_names)
    return 0 if result['inference_complete'] else 2


if __name__ == '__main__':
    raise SystemExit(main())
