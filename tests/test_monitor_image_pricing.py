from copy import deepcopy

import pytest

from utils.monitor_image_evidence import compare_reads
from utils.monitor_price_extract import extract_evidence
from monitor_price_plan_support import SOURCE, _graph, _offer, _evidence
from utils.monitor_price_plan import build_plan
from monitor_image_support import SERVICE_NAMES, image_offer


@pytest.mark.parametrize('quote',["Must receive 50 or more units to qualify", "Minimum 50 units", "50+ units", "50 units minimum"])
def test_minimum_purchase_variants_stay_terms_not_delivered_quantity(quote):
    from utils.monitor_price_plan_offer import _term_values
    terms=_term_values({'offer_raw_text':quote,'conditions':[]})
    assert terms['min_quantity']==50 and terms['min_unit']=='unit'


def test_same_service_distinct_packages_and_eligibility_are_matched_independently():
    variants=[]
    for quantity,condition in [(3,'For women'),(6,'For women'),(3,'For men')]:
        offer=image_offer();offer['items']=[{'item_name':'Laser','quantity':quantity,'unit_type':'treatment','unit_price':None}]
        offer['conditions']=[{'requirement':condition,'evidence':condition}]
        offer['offer_raw_text']=f'Laser package of {quantity} treatments. {condition}. $300'
        variants.append(offer)
    result=compare_reads({'offers':variants,'all_offers_extracted':True},
                         {'offers':deepcopy(variants),'all_offers_extracted':True})
    assert len(result['accepted'])==3 and result['complete']


def test_price_in_dom_with_image_only_minimum_is_shadowed(tmp_path,monkeypatch):
    text='Botox special $12/unit'
    dom=_offer(offer_raw_text=text,regular_price=None,discount_price=12,price_model='per_unit',
               items=[{'item_name':'Botox','quantity':None,'unit_price':12,'unit_type':'unit','service_area':None}])
    picture={**deepcopy(dom),'offer_raw_text':text+'. Minimum 50 units; all units must be used during same appointment',
             'min_quantity':50,'quantity_kind':'minimum','dual_read_verified':True,
             'conditions':[{'requirement':'Same appointment','evidence':'all units must be used during same appointment'}]}
    class Client:
        def create_json_response(self,*a,**kw):return {'offers':[dom],'all_offers_extracted':True}
    monkeypatch.setattr('utils.monitor_price_extract.blind_image_reads',lambda *a:{'accepted':[picture],'rejected':[],'complete':True})
    result=extract_evidence({'url':SOURCE['url']},{'url':SOURCE['url'],'text':text,'accessible':True,
        'complete':True,'image_review_required':True,'image_write_enabled':False},tmp_path,Client(),SERVICE_NAMES)
    assert result['offers']==[] and len(result['image_candidates'])==1
    assert result['image_policy']['hold_parent_fields'] and not result['complete']


def test_dom_image_price_conflict_holds_only_conflicting_candidate(tmp_path,monkeypatch):
    text='Botox special $17/unit\nFacial special $99'
    dom=_offer(offer_raw_text='Botox special $17/unit',regular_price=None,discount_price=17,
               items=[{'item_name':'Botox','quantity':None,'unit_price':17,'unit_type':'unit'}])
    facial=_offer(offer_raw_text='Facial special $99',regular_price=None,discount_price=99,
                  items=[{'item_name':'Facial','quantity':None,'unit_price':None,'unit_type':None}])
    picture={**deepcopy(dom),'offer_raw_text':'Botox special $12/unit','discount_price':12,'dual_read_verified':True}
    picture['items'][0]['unit_price']=12
    class Client:
        def create_json_response(self,*a,**kw):return {'offers':[dom,facial],'all_offers_extracted':True}
    monkeypatch.setattr('utils.monitor_price_extract.blind_image_reads',lambda *a:{'accepted':[picture],'rejected':[],'complete':True})
    result=extract_evidence({'url':SOURCE['url']},{'url':SOURCE['url'],'text':text,'accessible':True,
        'complete':True,'image_review_required':True,'image_write_enabled':True},tmp_path,Client(),SERVICE_NAMES)
    assert [o['offer_raw_text'] for o in result['offers']]==['Facial special $99']
    assert result['image_rejections'][-1]['reason']=='image_text_price_conflict'


@pytest.mark.parametrize('quote,expected',[
    ('Buy 3 sessions, Get the 4th session FREE',(3,4)),
    ('Buy 40 Units of Botox, Get 20 Units FREE ($13/unit: 60 Units Total)',(40,60)),
    ('Buy 5 vials, get the 6th for FREE',(5,6)),
    ('Two FREE vials with the purchase of 8 vials for ONLY $5,199',(8,10)),
    ('Buy 3 sessions get 1 syringe free',None)])
def test_paid_and_delivered_counts_are_separate(quote,expected):
    from utils.offer_evidence_validator import stated_paid_and_delivered, _quantity_supported
    counts=stated_paid_and_delivered(quote)
    assert counts==expected
    if expected:
        assert _quantity_supported(expected[1],quote)
        assert not _quantity_supported(expected[0],quote)


def test_free_units_normalize_charge_on_paid_units_without_dividing_twice():
    raw='Buy 40 units of Botox, get 20 units FREE ($13/unit: 60 units total). Regular price $13 per unit'
    offer=_offer(offer_raw_text=raw,regular_price=13,discount_price=13,price_model='per_unit',
                 items=[{'item_name':'Botox','service_id':12,'quantity':60,'unit_price':13,'unit_type':'unit'}])
    plan=build_plan(SOURCE,_graph(),_evidence([offer]),{})
    assert plan['status']=='ready',plan['review']
    master=next(op for op in plan['operations'] if op['table']=='promo_offer_master')
    item=next(op for op in plan['operations'] if op['table']=='promo_offer_items')
    assert master['values']['discount_price']==520 and master['values']['regular_price']==780
    assert item['values']['delivered_units']==60 and item['values']['unit_price']==pytest.approx(8.6667)


def test_free_session_package_preserves_total_and_has_no_allocated_unit_price():
    raw='Package Deals: Buy 3 sessions, Get the 4th session FREE. Botox ONLY $135'
    offer=_offer(offer_raw_text=raw,regular_price=None,discount_price=135,price_model='total',is_package=True,
                 items=[{'item_name':'Botox','service_id':12,'quantity':4,'unit_price':None,'unit_type':'session'}])
    plan=build_plan(SOURCE,_graph(),_evidence([offer]),{})
    assert plan['status']=='ready',plan['review']
    item=next(op for op in plan['operations'] if op['table']=='promo_offer_items')
    assert item['values']['delivered_units']==4 and item['values']['unit_price'] is None


def test_fixed_package_total_cannot_be_mislabelled_as_per_unit():
    raw='Botox package 2 syringes ONLY $549 (REG. $625)'
    offer=_offer(offer_raw_text=raw,discount_price=549,regular_price=625,price_model='per_unit',
                 items=[{'item_name':'Botox','service_id':12,'quantity':2,'unit_type':'syringe'}])
    plan=build_plan(SOURCE,_graph(),_evidence([offer]),{})
    assert plan['operations']==[] and plan['review'][-1]['reason']=='per_unit_price_without_stated_rate'


def test_explicit_unit_rate_is_not_written_as_package_total():
    raw='Botox special $12/unit, 40 units. Regular $15/unit'
    offer=_offer(offer_raw_text=raw,discount_price=12,regular_price=15,price_model='total',
                 items=[{'item_name':'Botox','service_id':12,'quantity':40,'unit_type':'unit'}])
    plan=build_plan(SOURCE,_graph(),_evidence([offer]),{})
    assert plan['status']=='ready',plan['review']
    master=next(op for op in plan['operations'] if op['table']=='promo_offer_master')
    assert master['values']['price_model']=='per_unit' and master['values']['discount_price']==480


def test_same_service_two_fixed_bundles_preserve_the_unique_quantity_offer_id():
    graph=_graph();graph['services'][0]['service_name']='Dermal Filler'
    first=graph['offers'][0];first.update(offer_raw_text='Any 2 syringes of Dermal Filler $150 OFF',
        discount_price=None,regular_price=None,discount_amount=150)
    graph['items'][0].update(item_name='Dermal Filler',delivered_units=2,unit_price=None)
    second=deepcopy(first);second.update(id=99,offer_raw_text='Any 3 syringes of Dermal Filler $250 OFF',discount_amount=250)
    graph['offers'].append(second)
    graph['items'].append({**graph['items'][0],'offer_item_id':98,'offer_id':99,'delivered_units':3})
    offer=_offer(offer_raw_text='Any 2 syringes of Dermal Filler $175 OFF',regular_price=None,discount_price=None,discount_amount=175,
        items=[{'item_name':'Dermal Filler','service_id':12,'quantity':2,'unit_type':'syringe','unit_price':None}])
    evidence=_evidence([offer],complete=False,image_policy={'version':1,'depends_on_images':False,'hold_parent_fields':True})
    plan=build_plan(SOURCE,graph,evidence,{})
    assert plan['status']=='ready',plan['review']
    master=[op for op in plan['operations'] if op['table']=='promo_offer_master']
    assert len(master)==1 and master[0]['id']==4 and master[0]['action']=='update'
    assert master[0]['values']['discount_amount']==175


def test_claim_format_normalization_keeps_decimal_currency_and_percentage_roles():
    from utils.monitor_offer_tiers import _visible_key
    assert _visible_key('$1,950 .55cc')==_visible_key('$1950 0.55cc')
    assert _visible_key('$12/unit')!=_visible_key('$1.2/unit')
    assert _visible_key('10% off')!=_visible_key('$10 off')


def test_free_quantity_clause_can_span_lines():
    from utils.offer_evidence_validator import stated_paid_and_delivered
    assert stated_paid_and_delivered('Buy 3 sessions,\nGet the 4th session FREE')==(3,4)
    assert stated_paid_and_delivered('Two FREE vials\nwith the purchase of 8 vials')==(8,10)
    assert stated_paid_and_delivered('Buy 2 syringes of filler - Get in 1 syringe FREE!')==(2,3)


@pytest.mark.parametrize('unit',['syringe','vial','session','treatment','area','ml','mg','half-syringe'])
def test_explicit_unit_rate_supports_existing_non_botox_units(unit):
    from utils.service_price_guard import infer_explicit_per_unit_price
    assert infer_explicit_per_unit_price(f'Special $99/{unit} (regular $120/{unit})')==99


def test_regular_abbreviation_and_thousands_are_not_sale_unit_rates():
    from utils.service_price_guard import infer_explicit_per_unit_price
    assert infer_explicit_per_unit_price('Jeuveau Only $9/unit (reg. price: $10/unit)')==9
    assert infer_explicit_per_unit_price('Sculptra $1,950/vial (reg. price: $2,400/vial)')==1950


def test_source_savings_contradiction_keeps_the_stated_prices_and_drops_the_savings_amount():
    offer=_offer(offer_raw_text='SAVE $250 Face + Neck Now $1,400 (Reg. $1,750)',
        discount_price=1400,regular_price=1750,discount_amount=250,
        items=[{'item_name':'CO2 laser','quantity':None,'unit_price':None}])
    graph=_graph();graph.update(promotions=[],offers=[],items=[],services=[])
    plan=build_plan(SOURCE,graph,_evidence([offer]),{})
    assert plan['status']=='ready' and plan['review']==[]
    master=next(op for op in plan['operations'] if op['table']=='promo_offer_master')['values']
    assert master['discount_price']==1400 and master['regular_price']==1750 and master['discount_amount'] is None


def test_explicit_quantity_bonus_retains_null_prices_and_real_delivered_count():
    offer=_offer(offer_raw_text='Buy 2 vials of Sculptra, Get 1 FREE!',
        discount_price=None,regular_price=None,discount_amount=None,discount_percent=None,
        items=[{'item_name':'Sculptra','quantity':3,'unit_type':'vial','unit_price':None}])
    graph=_graph();graph.update(promotions=[],offers=[],items=[],services=[])
    plan=build_plan(SOURCE,graph,_evidence([offer]),{})
    assert plan['status']=='ready',plan['review']
    master=next(op['values'] for op in plan['operations'] if op['table']=='promo_offer_master')
    item=next(op['values'] for op in plan['operations'] if op['table']=='promo_offer_items')
    assert all(master[k] is None for k in ('regular_price','discount_price','discount_amount','discount_percent'))
    assert item['delivered_units']==3 and item['unit_price'] is None and item['service_id'] is None


def test_regular_price_abbreviation_is_direct_evidence_for_allure_rf():
    offer=_offer(offer_raw_text='Radiofrequency Microneedling: $650 (reg. price: $750/treatment)',
        discount_price=650,regular_price=750,items=[{'item_name':'Radiofrequency Microneedling','quantity':None,'unit_price':None}])
    graph=_graph();graph.update(promotions=[],offers=[],items=[],services=[])
    plan=build_plan(SOURCE,graph,_evidence([offer]),{})
    assert plan['status']=='ready',plan['review']
    master=next(op['values'] for op in plan['operations'] if op['table']=='promo_offer_master')
    assert master['regular_price']==750 and master['discount_price']==650


def test_shop_discount_badge_can_floor_percent_without_overwriting_evidenced_prices():
    offer=_offer(offer_raw_text='Naked Boost | B12 Shot $59.00 $49.00 -16%',
        discount_price=49,regular_price=59,discount_percent=16,
        items=[{'item_name':'B12 Shot','quantity':None,'unit_price':None}])
    graph=_graph();graph.update(promotions=[],offers=[],items=[],services=[])
    plan=build_plan(SOURCE,graph,_evidence([offer]),{})
    assert plan['status']=='ready',plan['review']
    master=next(op['values'] for op in plan['operations'] if op['table']=='promo_offer_master')
    assert master['regular_price']==59 and master['discount_price']==49 and master['discount_percent']==16


def test_unsplit_unit_pricing_tiers_cannot_be_written_as_generic_total():
    raw='Botox special: New patients $12/unit; Spa members $11/unit; regular $13/unit'
    offer=_offer(offer_raw_text=raw,discount_price=11,regular_price=13,price_model='total',
                 items=[{'item_name':'Botox','service_id':12,'quantity':None,'unit_type':'unit'}])
    plan=build_plan(SOURCE,_graph(),_evidence([offer]),{})
    assert plan['operations']==[] and plan['review'][-1]['reason']=='ambiguous_unit_price_context'
