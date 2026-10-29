"""Offline truth validation and scoring; never executes a database plan."""
from __future__ import annotations

from collections import Counter
from copy import deepcopy
from decimal import Decimal, InvalidOperation
from pathlib import Path
import re

from PIL import Image

from utils.monitor_price_store import atomic_json, now


def validate_truth(manifest, truth):
    rows = manifest['samples']
    ids = [r['sample_id'] for r in rows]
    errors = []
    if len(ids) != len(set(ids)):
        errors.append('duplicate_sample_id')
    if set(ids) != set(truth['samples']):
        errors.append('manifest_truth_id_mismatch')
    splits = {}
    for row in rows:
        sid = row['sample_id']
        splits.setdefault(row['merchant'], set()).add(row['split'])
        sample = truth['samples'].get(sid)
        if not sample:
            continue
        if any(sample.get(k) != row[k] for k in ('sample_id', 'merchant', 'split', 'url')):
            errors.append(f'{sid}:identity_mismatch')
        adjudication = sample.get('adjudication', {})
        if adjudication.get('kind') not in {'offer', 'negative', 'context', 'ambiguous'}:
            errors.append(f'{sid}:missing_adjudication')
        if not sample.get('owner_verified_at') or not sample.get('screenshots'):
            errors.append(f'{sid}:missing_review_evidence')
        for shot in sample.get('screenshots', []):
            try:
                with Image.open(shot['path']) as image:
                    width, height = image.size
                region = shot['region']
                x, y, w, h = (region[k] for k in ('x', 'y', 'width', 'height'))
                if min(x, y) < 0 or min(w, h) <= 0 or x + w > width or y + h > height:
                    raise ValueError('outside_image')
                if not shot.get('capture_session_time'):
                    raise ValueError('missing_capture_time')
            except (OSError, KeyError, ValueError, TypeError) as exc:
                errors.append(f'{sid}:screenshot:{type(exc).__name__}')
    if any(len(values) != 1 for values in splits.values()):
        errors.append('merchant_crosses_splits')
    return {'valid': not errors, 'errors': errors, 'samples': len(ids),
            'splits': dict(Counter(r['split'] for r in rows))}


def freeze_truth(manifest, truth, output):
    receipt = validate_truth(manifest, truth)
    if not receipt['valid']:
        raise ValueError('truth_validation_failed: ' + ', '.join(receipt['errors'][:10]))
    frozen = deepcopy(truth)
    frozen.update(freeze_status='frozen', frozen_at=now(), reviewed_sample_count=len(frozen['samples']),
                  validation=receipt)
    atomic_json(Path(output), frozen)
    return frozen


def text_tokens(value):
    return re.findall(r'[a-z]+|\d+(?:\.\d+)?', str(value or '').casefold().replace(',', ''))


def same_number(a, b):
    if a is None or b is None:
        return a is b
    if isinstance(a, bool) or isinstance(b, bool):
        return type(a) is type(b) and a == b
    try:
        return Decimal(str(a)) == Decimal(str(b))
    except InvalidOperation:
        return a == b


def expand_facts(facts):
    """Independent options and qualification tiers count as distinct offers."""
    base = {k: v for k, v in facts.items() if k not in {'alternatives', 'tiers', 'offers'}}
    alternatives = facts.get('alternatives')
    # A mix-and-match delivered bundle is one purchase, not per-service quantities.
    if facts.get('composition_choice_not_fixed_per_service_quantity'):
        alternatives = None
    branches = alternatives or [None]
    for branch in branches:
        option = {**base, **(branch if isinstance(branch, dict) else {'item_name': branch} if branch else {})}
        if isinstance(branch, dict) and 'item_name' not in branch and (branch.get('name') or branch.get('scope')):
            option['item_name'] = branch.get('name') or branch['scope']
        tiers = (branch.get('tiers') if isinstance(branch, dict) else None) or facts.get('tiers') or [None]
        for tier in tiers:
            yield {**option, **(tier or {})}


def expected_offers(sample):
    if sample['adjudication']['kind'] not in {'offer', 'ambiguous'}:
        return []
    if sample['adjudication']['kind'] == 'ambiguous' and not sample.get('raw_text'):
        return []
    output = []
    excluded = set(sample['adjudication'].get('unscorable_fields', []))
    for index, facts in enumerate(expand_facts(sample['facts'])):
        fields = {}
        rate = next((facts[k] for k in ('sale_rate', 'unit_rate', 'printed_sale_rate') if facts.get(k) is not None), None)
        price_model = facts.get('price_model') or ('per_unit' if rate is not None else 'from' if facts.get('price_is_from') else None)
        if price_model:
            fields['price_model'] = price_model
        mapping = {
            'discount_price': ('sale_rate', 'unit_rate', 'printed_sale_rate') if rate is not None else ('sale_total', 'discount_price', 'starting_price'),
            'regular_price': ('regular_rate',) if rate is not None else ('regular_total', 'regular_price'),
            'discount_percent': ('discount_percent',), 'discount_amount': ('discount_amount',),
            'min_quantity': ('minimum_purchase_quantity', 'minimum_paid_quantity'),
            'quantity': ('delivered_quantity', 'fixed_delivered_quantity', 'fixed_quantity'),
            'campaign_start_date': ('campaign_start_date',), 'campaign_end_date': ('campaign_end_date',),
            'is_package': ('explicit_package',),
            'is_new_customer_required': ('is_new_customer_required', 'new_customer_only'),
            'is_membership_required': ('is_membership_required', 'membership_required', 'member_only'),
        }
        for target, keys in mapping.items():
            if target in excluded:
                continue
            for key in keys:
                if key in facts and key not in excluded and target not in excluded:
                    fields[target] = facts[key]
                    break
        if facts.get('price_unstated') or facts.get('price_fields_must_be_null'):
            fields.update(discount_price=None, regular_price=None)
        if facts.get('regular_price_not_inferred'):
            fields['regular_price'] = None
        # These describe a component-specific discount, not a flat transaction percentage.
        if facts.get('discount_scope') or facts.get('second_session_discount_percent'):
            fields.pop('discount_percent', None)
        if facts.get('membership_required') is not None:
            fields['is_membership_required'] = facts['membership_required']
        eligibility = list(dict.fromkeys([*sample['facts'].get('source_common_eligibility', []),
                                         *sample['facts'].get('eligibility', []), *facts.get('eligibility', [])]))
        quote = facts.get('raw_text') or sample.get('raw_text') or facts.get('body_locator') or ''
        if facts.get('body_locator'):
            quote = facts['body_locator']
        output.append({'expected_id': f"{sample['sample_id']}:{index}", 'sample_id': sample['sample_id'],
            'kind': sample['adjudication']['kind'], 'fields': fields,
            'name': facts.get('item_name') or facts.get('name') or facts.get('scope') or facts.get('body_locator') or '',
            'quote': quote,
            'eligibility': eligibility, 'requires_review': facts.get('requires_review', sample['adjudication'].get('requires_review', False)),
            'cannot_prove_removal': sample['adjudication'].get('cannot_prove_removal', True),
            'package_unit_prices_null': facts.get('item_unit_prices_must_be_null', False),
            'excluded_fields': sorted(excluded)})
    return output


def match_score(expected, offer):
    quote = set(text_tokens(expected['quote']))
    actual = set(text_tokens(offer.get('offer_raw_text')))
    name = set(text_tokens(expected['name']))
    item_words = set(text_tokens(' '.join(i.get('item_name') or '' for i in offer.get('items', []))))
    name_match = len(name & item_words) / len(name) if name else 0
    overlap = len(quote & actual) / len(quote | actual) if quote | actual else 0
    # Name alone never matches unrelated instances of a repeated service.
    if name_match > .5:
        return .5 * name_match + .5 * overlap
    return overlap


def match_offers(expected, offers, overrides=None):
    matches, used = {}, set()
    overrides = overrides or {}
    if set(overrides) - {e['expected_id'] for e in expected}:
        raise ValueError('unknown_matching_override')
    for index, want in enumerate(expected):
        eid = want['expected_id']
        if eid in overrides:
            target = overrides[eid]
            if target is not None:
                if type(target) is not int or not 0 <= target < len(offers) or target in used:
                    raise ValueError('invalid_matching_override')
                matches[index] = target
                used.add(target)
    edges = sorted(((match_score(want, offer), i, j) for i, want in enumerate(expected)
                    for j, offer in enumerate(offers) if want['expected_id'] not in overrides), reverse=True)
    for score, i, j in edges:
        if score >= .35 and i not in matches and j not in used:
            matches[i] = j
            used.add(j)
    return matches


def actual_field(offer, field):
    if field == 'quantity':
        items = offer.get('items', [])
        return items[0].get('quantity', items[0].get('delivered_units')) if len(items) == 1 else None
    return offer.get(field)


def score_job(samples, offers, plan, overrides=None):
    expected = [offer for sample in samples for offer in expected_offers(sample)]
    positives = [e for e in expected if e['kind'] == 'offer']
    reviewed = overrides is not None
    if reviewed:
        if not overrides.get('adjudicated_at') or set(overrides.get('matches', {})) != {e['expected_id'] for e in expected}:
            raise ValueError('incomplete_matching_adjudication')
        matches = match_offers(expected, offers, overrides['matches'])
        assigned = set(matches.values())
        fp = {int(i) for i in overrides.get('false_positives', [])}
        unscorable = {int(i) for i in overrides.get('unscorable_predictions', {})}
        if (assigned & fp or assigned & unscorable or fp & unscorable
                or assigned | fp | unscorable != set(range(len(offers)))
                or any(not reason for reason in overrides.get('unscorable_predictions', {}).values())):
            raise ValueError('unadjudicated_prediction')
    else:
        matches = match_offers(expected, offers)
        unscorable = set()
    matched_positive = sum(i in matches for i, e in enumerate(expected) if e['kind'] == 'offer')
    matched_ambiguous = {matches[i] for i, e in enumerate(expected) if e['kind'] == 'ambiguous' and i in matches}
    scored_predictions = len(offers) - len(matched_ambiguous | unscorable)
    fields, rows, errors = Counter(), [], []
    for index, want in enumerate(expected):
        target = matches.get(index)
        row = {**want, 'prediction_index': target,
               'matching_score': match_score(want, offers[target]) if target is not None else None, 'comparisons': []}
        if target is None:
            if want['kind'] == 'offer':
                errors.append({'expected_id': want['expected_id'], 'reason': 'offer_missed'})
            rows.append(row)
            continue
        offer = offers[target]
        for key, value in want['fields'].items():
            actual = actual_field(offer, key)
            correct = same_number(value, actual)
            fields[f'{key}:total'] += 1
            fields[f'{key}:correct'] += correct
            comparison = {'field': key, 'expected': value, 'actual': actual, 'correct': correct}
            row['comparisons'].append(comparison)
            if not correct:
                errors.append({'expected_id': want['expected_id'], 'reason': 'field_mismatch', **comparison})
        if want['package_unit_prices_null']:
            correct = all(i.get('unit_price') is None for i in offer.get('items', []))
            fields['package_unit_prices_null:total'] += 1
            fields['package_unit_prices_null:correct'] += correct
            if not correct:
                errors.append({'expected_id': want['expected_id'], 'reason': 'package_price_allocated'})
        # Strict transcript coverage; paraphrases are explicitly reported as such.
        transcript = set(text_tokens(' '.join([offer.get('offer_raw_text', ''),
            *[c.get('evidence', '') if isinstance(c, dict) else str(c) for c in offer.get('conditions', [])]])))
        for condition in want['eligibility']:
            tokens = set(text_tokens(condition))
            correct = tokens.issubset(transcript)
            fields['qualification_transcript:total'] += 1
            fields['qualification_transcript:correct'] += correct
            if not correct:
                errors.append({'expected_id': want['expected_id'], 'reason': 'qualification_not_fully_transcribed', 'condition': condition})
        rows.append(row)
    unmatched = [i for i in range(len(offers)) if i not in set(matches.values()) | unscorable]
    for index in unmatched:
        errors.append({'prediction_index': index, 'reason': 'false_positive' if reviewed else 'unmatched_prediction_requires_adjudication'})
    removals = [op for op in plan.get('operations', []) if op.get('values', {}).get('is_active') is False]
    insufficient = any(s['adjudication'].get('cannot_prove_removal') for s in samples)
    negative_only = all(s['adjudication']['kind'] in {'negative', 'context'} for s in samples)
    return {'counts': {'true_positive': matched_positive, 'false_negative': len(positives) - matched_positive,
        'false_positive': scored_predictions - matched_positive, 'predictions': scored_predictions,
        'positive_offers': len(positives), 'ambiguous_predictions_excluded': len(matched_ambiguous | unscorable),
        'negative_only_source': int(negative_only),
        'negative_only_false_predictions': len(offers) if negative_only else 0,
        'insufficient_evidence_source': int(insufficient), 'unsafe_removal_operations': len(removals) if insufficient else 0},
        'fields': dict(fields), 'matches': rows, 'errors': errors,
        'matching_status': 'reviewer_confirmed' if reviewed else 'provisional_needs_review'}


def summarize(results):
    counts, fields = Counter(), Counter()
    for result in results:
        counts.update(result['counts'])
        fields.update(result['fields'])
    tp, fp, fn = (counts[k] for k in ('true_positive', 'false_positive', 'false_negative'))
    return {'counts': dict(counts), 'precision': tp / (tp + fp) if tp + fp else None,
            'recall': tp / (tp + fn) if tp + fn else None,
            'field_accuracy': {key[:-6]: {'correct': fields[key[:-6] + ':correct'], 'total': value,
                'accuracy': fields[key[:-6] + ':correct'] / value if value else None}
                for key, value in fields.items() if key.endswith(':total')},
            'matching_status': 'reviewer_confirmed' if all(r['matching_status'] == 'reviewer_confirmed' for r in results) else 'provisional_needs_review'}
