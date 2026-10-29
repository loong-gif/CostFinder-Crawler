"""Independent evaluator checks catch truth leakage and misleading denominators."""
import ast
from copy import deepcopy
from pathlib import Path

from PIL import Image
import pytest

from utils.monitor_image_evaluation import (
    expected_offers, freeze_truth, match_offers, score_job, summarize, validate_truth,
)
from scripts.run_monitor_image_evaluation import prepare_browser, plans_for, run


def sample(**facts):
    return {'sample_id': 'S1', 'merchant': 'business_7', 'split': 'development',
        'url': 'https://clinic.example/specials', 'raw_text': 'Botox $12/unit minimum 40 units',
        'status': 'clear', 'owner_verified_at': '2026-10-14T00:00:00Z',
        'facts': {'item_name': 'Botox', 'sale_rate': 12, 'regular_rate': None,
                  'fixed_quantity': None, 'minimum_purchase_quantity': 40,
                  'campaign_end_date': None, **facts},
        'adjudication': {'kind': 'offer', 'requires_review': False,
                         'cannot_prove_removal': True, 'unscorable_fields': []}}


def files(tmp_path):
    shot = tmp_path / 'original.png'
    Image.new('RGB', (100, 200), 'white').save(shot)
    row = {'sample_id': 'S1', 'job_id': 'VR001', 'merchant': 'business_7',
           'split': 'development', 'url': 'https://clinic.example/specials',
           'observed_at': '2026-10-10T00:00:00Z', 'screenshot_paths': [str(shot)],
           'prior_quote_locator': 'SECRET_REFERENCE_ANSWER'}
    s = sample()
    s['screenshots'] = [{'path': str(shot), 'region': {'x': 0, 'y': 0, 'width': 100, 'height': 200},
                         'capture_session_time': row['observed_at']}]
    return {'samples': [row]}, {'samples': {'S1': s}}


def offer(**changes):
    return {'offer_raw_text': 'Botox $12/unit minimum 40 units', 'discount_price': 12,
            'regular_price': None, 'price_model': 'per_unit', 'min_quantity': 40,
            'items': [{'item_name': 'Botox', 'quantity': None}], **changes}


def test_freeze_rejects_missing_review_invalid_region_and_cross_merchant_split(tmp_path):
    m, t = files(tmp_path)
    assert validate_truth(m, t)['valid']
    frozen = freeze_truth(m, t, tmp_path / 'frozen.json')
    assert frozen['freeze_status'] == 'frozen' and 'freeze_status' not in t
    t['samples']['S1']['screenshots'][0]['region']['height'] = 201
    with pytest.raises(ValueError, match='truth_validation_failed'):
        freeze_truth(m, t, tmp_path / 'bad.json')
    t['samples']['S1']['screenshots'][0]['region']['height'] = 200
    m['samples'].append({**m['samples'][0], 'sample_id': 'S2', 'split': 'held_out'})
    t['samples']['S2'] = {**deepcopy(t['samples']['S1']), 'sample_id': 'S2', 'split': 'held_out'}
    assert 'merchant_crosses_splits' in validate_truth(m, t)['errors']


def test_model_browser_cannot_contain_locator_truth_or_fabricated_coverage(tmp_path):
    m, t = files(tmp_path)
    browser = prepare_browser(m['samples'], tmp_path / 'inputs')
    assert 'SECRET_REFERENCE_ANSWER' not in str(browser)
    assert browser['text'] == '' and browser['complete'] is False
    assert len(browser['image_tiles']['a']) == len(browser['image_tiles']['b']) == 1


def test_minimum_rate_null_year_and_unscorable_amount_are_separate_fields():
    s = sample(sale_total=480)
    want = expected_offers(s)[0]
    assert want['fields']['discount_price'] == 12 and want['fields']['quantity'] is None
    assert want['fields']['min_quantity'] == 40 and want['fields']['campaign_end_date'] is None
    s['adjudication']['unscorable_fields'] = ['sale_rate', 'discount_price']
    assert 'discount_price' not in expected_offers(s)[0]['fields']


def test_tiers_alternatives_and_mix_and_match_have_correct_offer_denominators():
    s = sample(alternatives=[{'name': 'Botox', 'sale_rate': 12}, {'name': 'Dysport', 'sale_rate': 4}],
               tiers=[{'membership_required': False}, {'membership_required': True}])
    assert len(expected_offers(s)) == 4
    s['facts']['composition_choice_not_fixed_per_service_quantity'] = True
    s['facts'].pop('tiers')
    assert len(expected_offers(s)) == 1


def test_misses_do_not_disappear_and_ambiguous_cases_do_not_become_false_positives():
    a = sample()
    b = deepcopy(a)
    b.update(sample_id='S2', raw_text='Dysport $4/unit')
    b['facts']['item_name'] = 'Dysport'
    b['adjudication']['kind'] = 'ambiguous'
    score = score_job([a, b], [offer(), offer(offer_raw_text='Dysport $4/unit', items=[{'item_name': 'Dysport'}])], {})
    assert score['counts']['true_positive'] == 1
    assert score['counts']['ambiguous_predictions_excluded'] == 1
    assert score['counts']['false_positive'] == 0
    missed = score_job([a], [], {})
    assert summarize([missed])['recall'] == 0
    assert summarize([missed])['precision'] is None


def test_matching_overrides_cannot_reuse_predictions():
    expected = [expected_offers(sample())[0], {**expected_offers(sample())[0], 'expected_id': 'S2:0'}]
    with pytest.raises(ValueError, match='invalid_matching_override'):
        match_offers(expected, [offer()], {'S1:0': 0, 'S2:0': 0})


def test_confirmed_metrics_require_review_of_all_truth_and_extra_predictions():
    with pytest.raises(ValueError, match='incomplete_matching_adjudication'):
        score_job([sample()], [offer()], {}, {})
    reviewed = {'adjudicated_at': '2026-10-14', 'matches': {'S1:0': 0}, 'false_positives': []}
    assert score_job([sample()], [offer()], {}, reviewed)['matching_status'] == 'reviewer_confirmed'
    with pytest.raises(ValueError, match='unadjudicated_prediction'):
        score_job([sample()], [offer(), offer()], {}, reviewed)
    reviewed['unscorable_predictions'] = {'1': 'Unrelated unreadable historical image; explicitly unscorable.'}
    result = score_job([sample()], [offer(), offer()], {}, reviewed)
    assert result['counts']['predictions'] == 1


def test_synthetic_existing_sibling_is_preserved_when_capture_incomplete(tmp_path):
    m, t = files(tmp_path)
    evidence = {'url': m['samples'][0]['url'], 'accessible': True, 'complete': False,
                'markdown': '', 'offers': [], 'observed_date': '2026-10-10',
                'image_review_complete': False}
    plan, probe = plans_for(m['samples'], evidence)
    assert not any(op['values'].get('is_active') is False for op in probe['operations'])


def test_entrypoint_does_not_import_or_call_execution_interfaces():
    source = Path('scripts/run_monitor_image_evaluation.py').read_text()
    tree = ast.parse(source)
    forbidden = {'apply_plan', 'recover_journal', 'SupabaseRestClient', 'run_monitor', 'database_snapshot'}
    assert not any(isinstance(n, ast.Name) and n.id in forbidden for n in ast.walk(tree))
    assert not any(isinstance(n, ast.ImportFrom) and n.module and
                   ('rest_writer' in n.module or 'supabase' in n.module or 'run_monitor_price_sync' in n.module)
                   for n in ast.walk(tree))


def test_unfrozen_truth_never_calls_model(tmp_path):
    class Client:
        model = 'fake'
        def create_json_response_with_images(self, *args, **kwargs):
            pytest.fail('model called before freeze')
    m, t = files(tmp_path)
    with pytest.raises(ValueError, match='truth_not_frozen'):
        run(m, t, tmp_path / 'evaluation', 'development', 'first', Client())


def test_full_run_persists_first_outputs_and_never_replays_completed_jobs(tmp_path):
    class Client:
        model = 'fake'
        calls = 0
        def create_json_response_with_images(self, prompt, **kwargs):
            assert 'SECRET_REFERENCE_ANSWER' not in prompt
            self.calls += 1
            return {'offers': [], 'all_offers_extracted': True}, {'model': self.model, 'raw_response': {'offers': []}}
        def create_json_response(self, messages, **kwargs):
            assert 'SECRET_REFERENCE_ANSWER' not in str(messages)
            self.calls += 1
            return {'offers': [], 'all_offers_extracted': True, 'no_current_offers_quote': None}
    m, t = files(tmp_path)
    t = freeze_truth(m, t, tmp_path / 'truth.json')
    client = Client()
    result = run(m, t, tmp_path / 'run', 'development', 'first', client, service_names=['Botox', 'Others'])
    assert result['inference_complete'] and result['raw']['recall'] == 0
    assert result['raw']['counts']['unsafe_removal_operations'] == 0
    assert result['database_writes'] == 0
    count = client.calls
    run(m, t, tmp_path / 'run', 'development', 'first', client, service_names=['Botox', 'Others'])
    assert client.calls == count
    assert (tmp_path / 'run/VR001/attempt_01/image_read_a_0.json').is_file()
    assert (tmp_path / 'run/VR001/attempt_01/image_read_b_0.json').is_file()
