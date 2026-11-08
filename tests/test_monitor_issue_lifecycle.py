"""Actual extraction and scheduling regressions for the issue-log policy."""
from copy import deepcopy
from datetime import date, timedelta

from scripts.run_monitor_price_sync import (
    data_quality_recheck_urls,
    enqueue_data_quality_rechecks,
    enqueue_expired_campaign_checks,
    expired_active_promotion_urls,
)
from utils.monitor_price_extract import extract_evidence, visible_variant_offers
from utils.monitor_price_plan import build_plan
from utils.monitor_price_store import atomic_json, now
from utils.monitor_rendered_browser import BREAKPOINTS, combine_breakpoint_reports, render_page
from utils.rendered_visibility_gate import current_page_manifest, filter_visible_membership_benefits
from monitor_price_plan_support import SOURCE, RAW, _graph, _evidence, _offer
from test_monitor_price_pipeline import FakeLLM

SERVICE_NAMES = ["Botox", "Dermal Filler", "Others"]


def coverage(complete=True):
    return {name:{'width':width,'complete':complete,'accessible':True} for name,width,_ in BREAKPOINTS}


def test_only_the_desktop_viewport_is_rendered():
    assert [(name, width) for name, width, _ in BREAKPOINTS] == [('desktop', 1440)]


def test_breakpoint_union_keeps_offer_text_and_incomplete_port_blocks_absence():
    reports=[{'breakpoint':name,'width':width,'accessible':True,'complete':True,'text':RAW,'scroll_complete':True}
             for name,width,_ in BREAKPOINTS]
    combined=combine_breakpoint_reports(SOURCE['url'],reports)
    assert RAW in combined['text'] and combined['complete']
    reports[0]['complete']=False
    combined=combine_breakpoint_reports(SOURCE['url'],reports)
    assert RAW in combined['text'] and not combined['complete']
    # 证据里出现不在 BREAKPOINTS 中的宽度（旧版三宽度渲染的结果）时覆盖不算完整
    legacy=reports+[{'breakpoint':'mobile','width':390,'accessible':True,'complete':True,'text':'Header','scroll_complete':True}]
    legacy[0]['complete']=True
    assert not combine_breakpoint_reports(SOURCE['url'],legacy)['complete']


def test_a_recent_render_with_another_breakpoint_set_is_rendered_again():
    from utils.monitor_image_gate import capture_is_current
    current={'captured_at':now(),'breakpoint_coverage':coverage()}
    assert capture_is_current(current)
    legacy={'captured_at':now(),'breakpoint_coverage':{**coverage(),'tablet':{'complete':True},'mobile':{'complete':True}}}
    assert not capture_is_current(legacy)


def test_renderer_visits_every_configured_breakpoint_once(tmp_path,monkeypatch):
    visits=[]
    def capture(url,directory,**options):
        visits.append(options['width'])
        return {'url':url,'accessible':True,'complete':True,'text':str(options['width']),'captured_at':now(),'scroll_complete':True}
    monkeypatch.setattr('utils.monitor_rendered_browser._render_page',capture)
    report=render_page(SOURCE['url'],tmp_path)
    assert visits==[width for _,width,_ in BREAKPOINTS] and report['complete']
    assert set(report['breakpoint_coverage'])=={name for name,_,_ in BREAKPOINTS}


def test_complete_empty_page_flows_through_extractor_then_retires_existing_offer(tmp_path):
    browser={'url':SOURCE['url'],'accessible':True,'complete':True,'text':'Our clinic welcomes you',
             'image_review_required':False,'breakpoint_coverage':coverage()}
    evidence=extract_evidence({'url':SOURCE['url']},browser,tmp_path,FakeLLM([]),SERVICE_NAMES)
    assert evidence['complete'] and not evidence['explicitly_empty']
    plan=build_plan(SOURCE,_graph(),evidence,{})
    assert plan['status']=='ready',plan
    assert any(op['table']=='promo_offer_master' and op['values'].get('is_active') is False for op in plan['operations'])


def test_partial_breakpoint_even_empty_extraction_cannot_retire(tmp_path):
    browser={'url':SOURCE['url'],'accessible':True,'complete':False,'text':'Our clinic welcomes you',
             'image_review_required':False,'breakpoint_coverage':coverage(False)}
    evidence=extract_evidence({'url':SOURCE['url']},browser,tmp_path,FakeLLM([]),SERVICE_NAMES)
    plan=build_plan(SOURCE,_graph(),evidence,{})
    assert plan['status']=='review' and not plan['operations']


def test_expired_campaign_is_retired_only_when_current_identity_and_coverage_match():
    graph=_graph();graph['promotions'][0].update(promotion_title='September specials',campaign_end_date='2026-09-30')
    evidence=_evidence(promotion_title='September specials',campaign_end_date='2026-09-30',observed_date='2026-10-14',all_offers_extracted=True,
                       breakpoint_coverage=coverage())
    plan=build_plan(SOURCE,graph,evidence,{})
    assert plan['status']=='ready',plan
    assert any(op['table']=='clinic_promotions' and op['values'].get('is_active') is False for op in plan['operations'])
    evidence['breakpoint_coverage']['desktop']['complete']=False
    plan=build_plan(SOURCE,graph,evidence,{})
    assert not any(op['values'].get('is_active') is False for op in plan['operations'])


def test_expired_active_promotion_urls_includes_unconfigured_source():
    source = "https://clinic.example/unlisted-specials"
    snapshot = {
        "promotions": [
            {
                "promotion_id": 99,
                "is_active": True,
                "source_url": source,
                "campaign_end_date": (date.today() - timedelta(days=2)).isoformat(),
            }
        ]
    }
    assert expired_active_promotion_urls(snapshot) == {source}


def test_expiry_rechecks_new_capture_without_permanent_event_dedupe(tmp_path):
    source=SOURCE['url'];config={'monitors':{'daily':{'targets':[{'urls':[source]}]}}}
    snapshot={'promotions':[{'promotion_id':3,'is_active':True,'source_url':source,
                            'campaign_end_date':(date.today()-timedelta(days=1)).isoformat()}]}
    atomic_json(tmp_path/'evidence/first.json',{'url':source,'captured_at':now(),'markdown':RAW})
    assert enqueue_expired_campaign_checks(tmp_path,snapshot,config)==1
    assert enqueue_expired_campaign_checks(tmp_path,snapshot,config)==0
    atomic_json(tmp_path/'evidence/second.json',{'url':source,'captured_at':now(),'markdown':RAW})
    assert enqueue_expired_campaign_checks(tmp_path,snapshot,config)==1


def test_shared_recheck_queue_does_not_consume_member_or_service_entries(tmp_path):
    entries=[{'source_url':SOURCE['url'],'source_kind':'promotion','status':'pending','requested_at':now()},
             {'source_url':'https://clinic.example/membership','source_kind':'membership','status':'pending'},
             {'source_url':'https://clinic.example/services','source_kind':'service','status':'pending'}]
    atomic_json(tmp_path/'data_quality/recheck_queue.json',{'entries':entries})
    atomic_json(tmp_path/'evidence/fresh.json',{'url':SOURCE['url'],'captured_at':now(),'markdown':RAW})
    assert data_quality_recheck_urls(tmp_path)=={SOURCE['url']}
    assert enqueue_data_quality_rechecks(tmp_path)==1
    assert enqueue_data_quality_rechecks(tmp_path)==0


def test_member_image_label_needs_current_readable_capture_and_exact_price(tmp_path):
    shot=tmp_path/'proof.png';shot.write_bytes(b'png')
    browser={'url':'https://clinic.example/membership','captured_at':now(),'accessible':True,'complete':True,
             'image_review_required':True,'screenshot_path':str(shot),'text':'Membership','breakpoint_coverage':coverage()}
    claim='One facial per month for $99'
    manifest=current_page_manifest(browser,'membership',[claim],image_transcript=claim,image_complete=True)
    assert filter_visible_membership_benefits([claim],manifest).accepted
    manifest['claimResults'][0]['visibleEvidence']['captured_at']='old'
    assert not filter_visible_membership_benefits([claim],manifest).accepted
    manifest=current_page_manifest(browser,'membership',[claim],image_transcript='___ $9 !!',image_complete=True)
    assert not filter_visible_membership_benefits([claim],manifest).accepted


def test_visible_variants_keep_member_eligibility_count_and_unpriced_missing_count(tmp_path):
    shot=tmp_path/'variant.png';shot.write_bytes(b'png')
    captures=[{'purchasable':True,'url':'https://clinic.example/products/tox','variant_id':str(i),'title':'Botox',
               'selected_options':[option],'discount_price':price,'regular_price':100,'screenshot_path':str(shot),
               'offer_raw_text':f'Botox\n{option}\nRegular price $100 Sale price ${price}','captured_at':now()}
              for i,option,price in [(1,'Member / 10 Units TOX Package',69.9),(2,'Non-Member / 10 Units TOX Package',79.9),(3,'Undereye',89.9)]]
    offers=visible_variant_offers({'variant_captures':captures})
    assert len(offers)==3
    assert [o['is_membership_required'] for o in offers]==[True,False,False]
    assert (offers[0]['discount_price'],offers[0]['regular_price'])==(69.9,100)
    assert (offers[1]['discount_price'],offers[1]['regular_price'])==(79.9,100)
    assert [o['items'][0]['delivered_units'] for o in offers]==[10,10,None]
    assert offers[2]['items'][0]['item_name']=='Botox Undereye'


def test_member_pairs_non_member_across_collection_and_variant_urls(tmp_path):
    shot=tmp_path/'variant.png';shot.write_bytes(b'png')
    rows=[('https://shop.example/collections/offers/products/tox','Non-Member',79.9),
          ('https://shop.example/products/tox?variant=2','Member',69.9)]
    captures=[{'purchasable':True,'url':url,'variant_id':url[-1],'title':'TOX','selected_options':[tier,'10 Units TOX Package'],
               'discount_price':price,'regular_price':89.9,'screenshot_path':str(shot),
               'offer_raw_text':f'TOX {tier} 10 Units ${price}','captured_at':now()} for url,tier,price in rows]
    offers=visible_variant_offers({'variant_captures':captures})
    assert [(o['is_membership_required'],o['regular_price'],o['discount_price']) for o in offers]==[
        (False,89.9,79.9),(True,89.9,69.9)]


def test_exact_ego_capture_reuses_original_time_and_final_boundary_checks_receipt(tmp_path,monkeypatch):
    import pytest
    from scripts.run_monitor_price_sync import prepare_event
    from utils.monitor_image_gate import image_write_block
    shot=tmp_path/'ego.png';shot.write_bytes(b'png')
    captured=now();quote=RAW.replace('$350','$400')
    artifact={'url':SOURCE['url'],'captured_at':captured,'markdown':'![current poster](poster.png)'}
    artifact_path=tmp_path/'evidence/original.json';atomic_json(artifact_path,artifact)
    receipt={'url':SOURCE['url'],'artifact_captured_at':captured,'artifact_markdown':artifact['markdown'],
             'observed_at':captured,'job_id':'reviewed','page_status':'accessible','complete':True,
             'screenshots_viewed':[str(shot)],'evidence_files':[str(shot)],'verified_text':quote,
             'breakpoint_coverage':coverage()}
    atomic_json(tmp_path/'visual_verifications.json',[receipt])
    event_path=tmp_path/'events/human.json'
    atomic_json(event_path,{'url':SOURCE['url'],'event_id':'human','status':'pending','artifact_path':str(artifact_path)})
    snapshot={**_graph(),'businesses':[],'service_names':SERVICE_NAMES}
    monkeypatch.setattr('scripts.run_monitor_price_sync.render_page',lambda *a,**k:pytest.fail('exact reviewed capture must be reused'))
    monkeypatch.setattr('utils.monitor_price_extract.build_client_from_env',lambda:FakeLLM([_offer(offer_raw_text=quote,discount_price=400)]))
    plan=prepare_event(event_path,snapshot,{},tmp_path)
    assert plan['status']=='ready',plan
    from utils.monitor_price_store import read_json
    browser=read_json(tmp_path/'sync/human/browser.json')
    assert browser['captured_at']==captured
    assert browser['prior_visual_receipt']['observed_at']==captured
    assert image_write_block(plan,tmp_path/'journals') is None
    receipt['artifact_captured_at']='different capture'
    atomic_json(tmp_path/'visual_verifications.json',[receipt])
    assert image_write_block(plan,tmp_path/'journals')=='human_capture_proof_mismatch'


def test_direct_from_price_preserves_lower_bound_but_cannot_erase_other_variants():
    offer=_offer(offer_raw_text='Botox special from $350',regular_price=None,discount_price=350,price_model='from',
                 items=[{'item_name':'Botox','quantity':None,'service_id':12}])
    graph=_graph()
    plan=build_plan(SOURCE,graph,_evidence([offer]),{})
    assert plan['status']=='ready',plan
    assert plan['evidence_summary']['complete'] is False
    assert not any(op['values'].get('is_active') is False for op in plan['operations'])
    parents=[op for op in plan['operations'] if op['table']=='clinic_promotions']
    assert all('is_offer_extracted' not in op['values'] for op in parents)


def test_complete_image_review_can_create_dom_grounded_parent_without_image_offers(tmp_path):
    from utils.monitor_image_gate import image_write_block
    from utils.monitor_price_store import read_json
    from monitor_image_support import proof_plan
    plan,proof=proof_plan(tmp_path)
    browser=read_json(tmp_path/'browser.json');browser['text']=RAW
    atomic_json(tmp_path/'browser.json',browser)
    atomic_json(tmp_path/'evidence.json',{'offers':[_offer()]})
    proof['reads']={round_name:{'offers':[],'all_offers_extracted':True} for round_name in ('a','b')}
    atomic_json(tmp_path/'dual.json',proof)
    plan['operations']=[{'table':'clinic_promotions','action':'insert','values':{'promotion_content':[RAW]}}]
    plan['image_policy']['candidate_facts']=[]
    assert image_write_block(plan,tmp_path/'journals') is None
