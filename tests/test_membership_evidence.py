from utils.membership_evidence import prepare_membership_write
from utils.membership_plans import normalize_membership_plan, build_membership_plan_insert_row
from utils.clinic_services_firecrawl import validate_service


def test_missing_benefits_remain_unknown():
    plan = normalize_membership_plan({'plan_name': 'VIP', 'monthly_fee': 199})
    assert plan['benefits'] is None
    assert build_membership_plan_insert_row(plan, {})['benefits'] is None


def test_fee_cannot_borrow_another_tier():
    assert not prepare_membership_write({'membership_name':'VIP','membership_price':199}, 'VIP $299\nBasic $199')['accepted']


def test_markdown_quote_matches_split_visible_plan_and_fee():
    item = {'membership_name':'EMERALD','membership_price':289,'membership_description':'### EMERALD\n\n$289/Month'}
    page = {'status':'page_accessible','capture':{'nodes':[
        {'text':'EMERALD','visible':True,'rects':[{'y':100}],'ancestors':[{'tag':'H3'}]},
        {'text':'$289/Month','visible':True,'rects':[{'y':140}],'ancestors':[{'tag':'P'}]},
    ]}}
    decision = prepare_membership_write(item, 'EMERALD\n$289/Month', page)
    assert decision['accepted']


def test_fee_cannot_cross_the_next_visible_plan_heading():
    item = {'membership_name':'VIP','membership_price':199,'membership_description':'VIP $199'}
    page = {'status':'page_accessible','capture':{'nodes':[
        {'text':'VIP','visible':True,'rects':[{'y':100}],'ancestors':[{'tag':'H3'}]},
        {'text':'$299/Month','visible':True,'rects':[{'y':140}],'ancestors':[{'tag':'P'}]},
        {'text':'Basic','visible':True,'rects':[{'y':180}],'ancestors':[{'tag':'H3'}]},
        {'text':'$199/Month','visible':True,'rects':[{'y':220}],'ancestors':[{'tag':'P'}]},
    ]}}
    assert not prepare_membership_write(item, 'VIP $299\nBasic $199', page)['accepted']


def test_visible_benefits_survive_unverified_fee():
    item = {'membership_name':'VIP','membership_price':199,'benefits':['Monthly facial']}
    page = {'status':'page_accessible','claimResults':[
        {'recordType':'membership','claim':'Monthly facial','status':'visible_match'}]}
    decision = prepare_membership_write(item, 'VIP $299\nBasic $199\nMonthly facial', page)
    assert not decision['accepted']
    assert decision['benefits'] == item['benefits']


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
