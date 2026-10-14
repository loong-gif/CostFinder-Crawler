from utils.membership_evidence import prepare_membership_write
from utils.membership_plans import normalize_membership_plan, build_membership_plan_insert_row
from utils.clinic_services_firecrawl import validate_service


def test_missing_benefits_remain_unknown():
    plan = normalize_membership_plan({'plan_name': 'VIP', 'monthly_fee': 199})
    assert plan['benefits'] is None
    assert build_membership_plan_insert_row(plan, {})['benefits'] is None


def test_fee_cannot_borrow_another_tier():
    assert not prepare_membership_write({'membership_name':'VIP','membership_price':199}, 'VIP $299\nBasic $199')['accepted']


def test_rendered_benefits_and_incomplete_capture():
    item = {'membership_name':'VIP','membership_price':199,'benefits':['One facial monthly']}
    evidence = 'VIP $199 per month\nOne facial monthly'
    missing = prepare_membership_write(item, evidence)
    assert missing['accepted'] and missing['benefits'] is None and not missing['complete']
    page = {'status':'page_accessible','claimResults':[{'recordType':'membership','claim':'One facial monthly','status':'visible_match'}]}
    verified = prepare_membership_write(item, evidence, page)
    assert verified['benefits'] == item['benefits'] and verified['complete']


def test_empty_array_is_not_proof_of_no_benefits():
    decision = prepare_membership_write({'membership_name':'VIP','membership_price':199,'benefits':[]}, 'VIP $199')
    assert decision['benefits'] is None and not decision['complete']


def test_service_anatomy_and_units_cannot_be_guessed():
    item, reason = validate_service({'service_name':'Botox','service_name_raw':'Botox $12','regular_price':12,'unit_type':'unit','service_area':'face'}, source_url='https://example.com/pricing', evidence='Botox $12')
    assert reason == 'accepted' and item['service_area'] is None and item['unit_type'] is None
