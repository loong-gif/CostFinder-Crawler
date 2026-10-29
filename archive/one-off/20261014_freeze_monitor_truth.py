#!/usr/bin/env python3
"""Merge independent visual reviews and freeze this historical evaluation dataset."""
from __future__ import annotations

import csv
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from utils.monitor_image_evaluation import freeze_truth, validate_truth
from utils.monitor_price_store import atomic_json, now, read_json

DIRECTORY = ROOT / 'output/monitor_price_sync/image_accuracy_20261012'


def prior_adjudication(sample):
    status = sample['status']
    if status.startswith('clear'):
        kind = 'offer'
    elif status.startswith(('partial', 'contradictory')):
        raise ValueError('specialist_adjudication_missing:' + sample['sample_id'])
    elif status == 'context_only':
        kind = 'context'
    elif status in {'unproven_offer', 'unfinalized_specials', 'heading_only', 'price_unstated'}:
        kind = 'ambiguous'
    else:
        kind = 'negative'
    return {'kind': kind, 'requires_review': status == 'clear_identity_review' or kind == 'ambiguous',
        'unscorable_fields': [], 'cannot_prove_removal': True,
        'reason': 'Preserved prior original-screenshot review and its recorded status; no new visual verification claimed.',
        'provenance': 'prior_review_status', 'adjudicated_at': now()}


def merge(directory=DIRECTORY):
    manifest = read_json(directory / 'sample_manifest.json')
    truth = read_json(directory / 'truth_draft.json')
    if not (directory / 'truth_before_20261014.json').exists():
        atomic_json(directory / 'truth_before_20261014.json', truth)
        atomic_json(directory / 'manifest_before_20261014.json', manifest)
    rows = {row['sample_id']: row for row in manifest['samples']}
    contributed = set()
    receipts = []
    for owner in ('a', 'b', 'c'):
        review = read_json(directory / f'review_{owner}.json')
        if not review:
            raise ValueError('missing_review:' + owner)
        for addition in review.get('additions', []):
            sid = addition['sample_id']
            if sid in rows:
                raise ValueError('duplicate_addition:' + sid)
            rows[sid] = addition
            manifest['samples'].append(addition)
        for sid, sample in review['samples'].items():
            if sid in contributed:
                raise ValueError('overlapping_reviewer_ownership:' + sid)
            if sid not in rows:
                raise ValueError('review_sample_missing_manifest:' + sid)
            contributed.add(sid)
            truth['samples'][sid] = sample
            rows[sid].update(owner_review_status='verified_' + sample['status'],
                             truth_reference='truth_draft.json#' + sid,
                             screenshot_paths=list(dict.fromkeys(shot['path'] for shot in sample['screenshots'])))
        receipts.append({'reviewer': owner, 'samples': len(review['samples']),
                         'reviewed_jobs': review.get('reviewed_jobs', []), 'limitations': review.get('limitations', [])})
    for sample in truth['samples'].values():
        if 'adjudication' not in sample:
            sample['adjudication'] = prior_adjudication(sample)
    for sid, sample in read_json(directory / 'root_adjudications.json', {}).get('samples', {}).items():
        truth['samples'][sid] = sample
    truth.update(reviewed_sample_count=len(truth['samples']), updated_at=now(),
                 freeze_status='ready_for_validation', review_receipts=receipts)
    validation = validate_truth(manifest, truth)
    atomic_json(directory / 'freeze_validation.json', validation)
    if not validation['valid']:
        raise ValueError('validation_failed:' + ','.join(validation['errors'][:15]))
    frozen = freeze_truth(manifest, truth, directory / 'truth_frozen.json')
    manifest.update(freeze_status='frozen', frozen_at=frozen['frozen_at'],
                    owner_discovered_samples=sum(bool(row.get('owner_discovered')) for row in manifest['samples']))
    atomic_json(directory / 'truth_draft.json', truth)
    atomic_json(directory / 'sample_manifest.json', manifest)
    with (directory / 'sample_manifest.csv').open('w', newline='') as stream:
        writer = csv.DictWriter(stream, fieldnames=sorted({k for row in manifest['samples'] for k in row}))
        writer.writeheader()
        writer.writerows(manifest['samples'])
    print({'samples': len(manifest['samples']), 'freeze_status': frozen['freeze_status'], 'validation': validation})


if __name__ == '__main__':
    merge()
