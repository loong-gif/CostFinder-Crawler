"""Image-policy regressions: blind inputs, semantic disagreement and final DML boundary."""
from copy import deepcopy
import json
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from utils.monitor_image_evidence import compare_reads, critical_fields, blind_image_reads, pixel_digest, tile_ranges
from utils.monitor_image_gate import image_write_block
from utils.monitor_price_store import atomic_json, now
from utils.monitor_price_extract import extraction_schema, extract_evidence
from utils.offer_extraction_llm import GeminiNativeClient
from utils.monitor_pending_sql import render_pending
from utils.monitor_price_writer import render_apply
from test_monitor_price_plan import SOURCE, _graph, _offer, _evidence
from utils.monitor_price_plan import build_plan

SERVICE_NAMES = ['Botox', 'Dermal Filler', 'Others']
from test_monitor_price_writer import plan_for as rest_plan, state as writer_state


def image_offer():
    return {**_offer(),'terms_complete':True,'evidence_regions':[0],
            'campaign_start_date':None,'campaign_end_date':None,'campaign_title':None}


def reads(offer):
    return {'offers':[offer],'all_offers_extracted':True}


@pytest.mark.parametrize('field,value',[
    ('discount_price',17),('regular_price',12),('discount_percent',12),('discount_amount',12),
    ('price_model','per_unit'),('min_quantity',20),('quantity_kind','minimum'),('is_package',True),
    ('is_new_customer_required',True),('is_membership_required',True),('campaign_end_date','2026-10-31'),
    ('conditions',[{'requirement':'Same appointment','evidence':'Same appointment'}])])
def test_any_critical_field_disagreement_abstains(field,value):
    a=image_offer();b={**deepcopy(a),field:value}
    assert compare_reads(reads(a),reads(b))['accepted']==[]


@pytest.mark.parametrize('field,value',[('unit_price',7.1),('delivered_units',60),('unit_type','syringe'),('service_area','neck')])
def test_item_disagreement_abstains(field,value):
    a=image_offer();b=deepcopy(a)
    # Native schema uses quantity; planner adds delivered_units only afterward.
    if field=='delivered_units':field='quantity'
    b['items'][0][field]=value
    assert not compare_reads(reads(a),reads(b))['accepted']


def test_different_currency_decimal_and_percentage_quotes_abstain():
    a=image_offer()
    for changed in ['$17','$12.5','12%']:
        b={**a,'offer_raw_text':a['offer_raw_text'].replace('$350',changed)}
        assert not compare_reads(reads(a),reads(b))['accepted']


def test_terms_or_position_missing_abstains_without_hiding_other_offer():
    a=image_offer();b={**deepcopy(a),'terms_complete':False}
    assert not compare_reads(reads(a),reads(b))['accepted']
    good={**deepcopy(a),'items':[{'item_name':'Dysport','unit_price':12,'quantity':None}]}
    result=compare_reads({'offers':[a,good],'all_offers_extracted':True},
                         {'offers':[b,good],'all_offers_extracted':True})
    assert len(result['accepted'])==1 and result['accepted'][0]['items'][0]['item_name']=='Dysport'
    assert not result['complete']


def test_png_digest_ignores_metadata_and_compression_but_detects_pixels():
    from PIL import Image, PngImagePlugin
    import io
    outputs=[]
    for level,text,color in [(1,'one','red'),(9,'two','red'),(9,'two','blue')]:
        stream=io.BytesIO();meta=PngImagePlugin.PngInfo();meta.add_text('key',text)
        Image.new('RGB',(4,4),color).save(stream,format='PNG',compress_level=level,pnginfo=meta)
        outputs.append(pixel_digest(stream.getvalue()))
    assert outputs[0]==outputs[1] and outputs[0]!=outputs[2]


def test_both_tile_rounds_cover_every_pixel_with_shifted_boundaries():
    for height in (100,1600,1601,5000,40000):
        rounds=[tile_ranges(height,shifted=v) for v in (False,True)]
        for tiles in rounds:
            covered=set()
            for y,h in tiles:covered.update(range(y,y+h))
            assert covered==set(range(height)) and all(h<=1600 for _,h in tiles)
        if height>1600:assert rounds[0]!=rounds[1]


def test_two_blind_rounds_batch_limits_and_no_candidate_truth_or_db_inputs(tmp_path):
    browser={'url':'https://test','complete':True,'image_tiles':{'a':[],'b':[]}}
    for round_name in ('a','b'):
        for i in range(9):
            p=tmp_path/f'{round_name}{i}.png';p.write_bytes(b'png')
            browser['image_tiles'][round_name].append({'path':str(p)})
    class Client:
        calls=[]
        def create_json_response_with_images(self,prompt,**kw):
            self.calls.append((prompt,kw))
            return {'offers':[],'all_offers_extracted':True},{'model':'gemini-3.5-flash-lite','raw_response':{}}
    client=Client();result=blind_image_reads(browser,tmp_path,client,extraction_schema(SERVICE_NAMES))
    assert [len(k['images']) for _,k in client.calls]==[8,3,8,3]
    for _,call in client.calls:
        assert len(call['images'])<=8 and sum(len(data) for data,mime in call['images'])<=10*1024*1024
    assert len({p for p,_ in client.calls})==1
    assert all('truth' not in p.lower() and 'database' not in p.lower() for p,_ in client.calls)
    assert result['complete']


def test_native_batch_boundaries_and_single_image_compatibility():
    client=GeminiNativeClient('test','gemini-3.5-flash-lite')
    for images in ([],[(b'x','image/png')]*9,[(b'x'*(10*1024*1024+1),'image/png')]):
        with pytest.raises(ValueError):client.create_json_response_with_images('p',images=images)
    response=MagicMock();response.json.return_value={'candidates':[{'content':{'parts':[{'text':'{}'}]}}],
                                                     'modelVersion':'v1','usageMetadata':{'totalTokenCount':2}}
    with patch('utils.offer_extraction_llm.requests.post',return_value=response):
        value,metadata=client.create_json_response_with_images('p',images=[(b'x','image/png')],return_metadata=True)
        assert value=={} and metadata['model_version']=='v1' and metadata['usage']['totalTokenCount']==2
        assert client.create_json_response_with_image('p',image_bytes=b'x',mime_type='image/png')=={}


def test_legacy_receipt_is_image_evidence_even_when_helper_says_no_image(tmp_path):
    class Client:
        def create_json_response(self,*a,**kw):return {'offers':[_offer()], 'all_offers_extracted':True}
    browser={'accessible':True,'complete':True,'image_review_required':False,'text':_offer()['offer_raw_text'],
             'prior_visual_receipt':{'job_id':'old'},'image_write_enabled':False}
    evidence=extract_evidence({'url':SOURCE['url']},browser,tmp_path,Client(),SERVICE_NAMES)
    plan=build_plan(SOURCE,_graph(),evidence,{})
    assert plan['operations']==[] and evidence['image_policy']['hold_parent_fields']
    plan['evidence_path']=str(tmp_path/'evidence.json')
    assert image_write_block(plan,tmp_path/'journals')=='legacy_image_evidence_requires_refresh'


def test_shadow_keeps_parent_fields_and_image_siblings_while_text_price_updates():
    graph=_graph();image=deepcopy(graph['offers'][0]);image.update(id=99,offer_raw_text='Dysport $12/unit')
    graph['offers'].append(image)
    graph['items'].append({**graph['items'][0],'offer_item_id':98,'offer_id':99,'item_name':'Dysport','service_id':97})
    graph['services'].append({'service_id':97,'business_id':7,'service_name':'Dysport','service_area':None})
    raw=_offer(discount_price=400,offer_raw_text=_offer()['offer_raw_text'].replace('$350','$400'))
    evidence=_evidence([raw],complete=False,image_policy={'version':1,'depends_on_images':False,'hold_parent_fields':True})
    plan=build_plan(SOURCE,graph,evidence,{})
    assert plan['status']=='ready'
    parent=next(op for op in plan['operations'] if op['table']=='clinic_promotions')
    assert parent['values']=={'is_offer_extracted':False}
    sql=render_apply(plan,{})
    updates=[line for line in sql.splitlines() if line.strip().startswith('update public.')]
    assert updates and not any('where id = 99' in line or 'offer_item_id = 98' in line for line in updates)
    # The image sibling is version-checked after the writes instead of being touched.
    assert "(99, " in next(l for l in sql.splitlines() if 'unexpected_row_or_trigger_change: promo_offer_master' in l)
    assert next(op for op in plan['operations'] if op['table']=='promo_offer_master')['values']['discount_price']==400


def proof_plan(tmp_path, *, complete=True):
    candidate=image_offer();a=tmp_path/'a.png';b=tmp_path/'b.png';a.write_bytes(b'png');b.write_bytes(b'png')
    one={**candidate,'region_files':[{'path':str(a)}]};two={**deepcopy(candidate),'region_files':[{'path':str(b)}]}
    browser={'url':'https://test','visual_digest':'pixels','context_digest':'terms','complete':complete,'captured_at':now(),'breakpoint_coverage':{n:{'complete':complete} for n in ('desktop','tablet','mobile')}}
    atomic_json(tmp_path/'browser.json',browser);atomic_json(tmp_path/'evidence.json',{})
    proof={'calls':[{'round':'a','regions':[{'path':str(a)}]},{'round':'b','regions':[{'path':str(b)}]}],'reads':{'a':reads(one),'b':reads(two)},
           'complete':complete,'coverage_complete':complete,**browser}
    atomic_json(tmp_path/'dual.json',proof);atomic_json(tmp_path/'price_sync_control.json',{'image_write_enabled':True})
    plan={'evidence_path':str(tmp_path/'evidence.json'),'operations':[],
          'image_policy':{'depends_on_images':True,'dual_read_verified':True,'dual_read_path':str(tmp_path/'dual.json'),
                          'candidate_facts':[critical_fields(candidate)]}}
    return plan,proof


def test_final_boundary_requires_both_round_files_and_matching_capture(tmp_path):
    plan,proof=proof_plan(tmp_path)
    assert image_write_block(plan,tmp_path/'journals') is None
    (tmp_path/'b.png').unlink()
    assert image_write_block(plan,tmp_path/'journals')=='image_region_missing'
    atomic_json(tmp_path/'browser.json',{'url':'https://other'})
    assert image_write_block(plan,tmp_path/'journals')=='image_capture_proof_mismatch'


def test_empty_candidates_incomplete_proof_cannot_deactivate_or_change_parent(tmp_path):
    plan,proof=proof_plan(tmp_path,complete=False)
    for op in ({'table':'promo_offer_master','values':{'is_active':False}},
               {'table':'clinic_promotions','values':{'promotion_content':[]}}):
        plan['operations']=[op];plan['image_policy']['candidate_facts']=[]
        proof['reads']={'a':{'offers':[],'all_offers_extracted':False},'b':{'offers':[],'all_offers_extracted':False}}
        atomic_json(tmp_path/'dual.json',proof)
        assert image_write_block(plan,tmp_path/'journals')=='breakpoint_coverage_incomplete'


def test_image_switch_off_prevents_pending_sql_without_leaving_a_file(tmp_path):
    plan=rest_plan()
    plan['image_policy']={'depends_on_images':True,'dual_read_verified':True}
    receipt=render_pending(plan,{},tmp_path)
    assert receipt['category']=='image_writes_disabled' and receipt['status']=='review'
    assert not (tmp_path/'pending_sql').exists()


def test_global_pause_stops_text_sql_at_the_final_boundary(tmp_path):
    plan=rest_plan()
    plan['image_policy']={'depends_on_images':False}
    atomic_json(tmp_path/'price_sync_control.json',{'write_enabled':False,'image_write_enabled':True})
    assert render_pending(plan,{},tmp_path)['category']=='writes_disabled'
    assert not (tmp_path/'pending_sql').exists()


def test_pause_after_a_rendered_file_blocks_further_files_and_keeps_the_rendered_one(tmp_path):
    plan=rest_plan();plan['image_policy']={'depends_on_images':False};plan['event_id']='first'
    receipt=render_pending(plan,{},tmp_path)
    assert receipt['status']=='pending_sql' and Path(receipt['sql_path']).is_file()
    atomic_json(tmp_path/'price_sync_control.json',{'write_enabled':False})
    second=dict(plan,event_id='second')
    assert render_pending(second,{},tmp_path)['category']=='writes_disabled'
    assert [p.name.split('_',1)[1] for p in (tmp_path/'pending_sql').glob('*.sql')]==['first.sql']


def test_hidden_iframe_skips_subtree_and_query_failure_stays_partial():
    from utils.monitor_rendered_browser import frame_regions
    class Page:
        checked=[]
        def call(self,method,**params):
            if method=='DOM.getFrameOwner':
                self.checked.append(params['frameId'])
                if params['frameId']=='unknown':raise RuntimeError('frame_not_resolved')
                return {'backendNodeId':1}
            if method=='DOM.resolveNode':return {'object':{'objectId':'owner'}}
            return {'result':{'value':{'visible':False}}}
    tree={'childFrames':[{'frame':{'id':'hidden','url':'https://hidden'},
              'childFrames':[{'frame':{'id':'grandchild','url':'https://not-read'}}]},
              {'frame':{'id':'unknown','url':'https://unknown'}}]}
    page=Page();regions=frame_regions(page,tree)
    assert page.checked==['hidden','unknown']
    assert len(regions)==1 and regions[0]['visibility_known'] is False


def test_obscured_dom_and_dual_agreement_cannot_release_hidden_price(tmp_path,monkeypatch):
    candidate=image_offer()
    monkeypatch.setattr('utils.monitor_price_extract.blind_image_reads',lambda *a:{
        'accepted':[candidate],'rejected':[],'complete':True})
    class Client:
        def create_json_response(self,messages,**kw):
            assert candidate['offer_raw_text'] not in messages[1]['content']
            return {'offers':[candidate],'all_offers_extracted':True}
    browser={'accessible':True,'complete':False,'text':candidate['offer_raw_text'],
             'failures':['image_obscured'],'image_review_required':True,'image_write_enabled':True}
    evidence=extract_evidence({'url':'https://test'},browser,tmp_path,Client(),SERVICE_NAMES)
    assert not evidence['image_candidates'] and not evidence['complete']
    assert all(o['visibility']=='unverified' for o in evidence['offers'])
    assert evidence['image_rejections'][0]['reason']=='image_obscured'


def test_image_proof_age_uses_capture_time_not_new_plan_timestamp(tmp_path):
    from datetime import datetime, timedelta, timezone
    plan,proof=proof_plan(tmp_path)
    browser=json.loads((tmp_path/'browser.json').read_text())
    browser['captured_at']=(datetime.now(timezone.utc)-timedelta(hours=25)).isoformat()
    atomic_json(tmp_path/'browser.json',browser)
    plan['prepared_at']=now()
    assert image_write_block(plan,tmp_path/'journals')=='image_capture_expired_requires_refresh'


def test_shadow_images_allow_new_independent_text_parent_without_reactivating_history():
    graph=_graph();graph['promotions'][0]['is_active']=False;graph['offers'][0]['is_active']=False
    evidence=_evidence(complete=False,image_policy={'version':1,'depends_on_images':False,'hold_parent_fields':True})
    plan=build_plan(SOURCE,graph,evidence,{})
    assert plan['status']=='ready',plan['review']
    parent=next(op for op in plan['operations'] if op['table']=='clinic_promotions')
    assert parent['action']=='insert' and parent['values']['is_offer_extracted'] is False
    assert all(op['action']=='insert' for op in plan['operations'])


def test_same_image_url_changed_pixels_enqueues_even_with_identical_markdown(tmp_path,monkeypatch):
    from scripts.firecrawl_self_hosted_monitor import run_monitor
    class Monitor:
        def __init__(self,*a):pass
        def scrape_document(self,url):return {'success':True,'data':{'markdown':'![poster](same.png)'}}
    captured=[]
    def render(url,directory):
        captured.append(url)
        return {'url':url,'accessible':True,'complete':True,'text':'Specials','visual_digest':str(len(captured)),
                'context_digest':'same terms','http_status':200,'image_review_required':True}
    monkeypatch.setattr('scripts.firecrawl_self_hosted_monitor.SelfHostedMonitor',Monitor)
    monkeypatch.setattr('utils.monitor_rendered_browser.render_page',render)
    config={'monitors':{'daily':{'targets':[{'urls':['https://test/specials']}]}}}
    for i in range(2):run_monitor(config,'daily',api_url='x',api_key='',state_dir=tmp_path,timeout=1,
                limit=None,workers=2,send_notifications=False,scan_images=True)
    events=[json.loads(p.read_text()) for p in (tmp_path/'events').glob('*.json')]
    assert len(events)==2 and all(e['change_type']=='visible_content_changed' for e in events)
    assert json.loads((tmp_path/'daily.json').read_text())['pages']['https://test/specials']['visual_digest']=='2'


def test_firecrawl_failure_falls_back_to_complete_pure_image_page(tmp_path,monkeypatch):
    from scripts.firecrawl_self_hosted_monitor import run_monitor, ScrapeFailure
    class Monitor:
        def __init__(self,*a):pass
        def scrape_document(self,url):raise ScrapeFailure('firecrawl_empty_markdown',{'success':True,'data':{'markdown':''}})
    monkeypatch.setattr('scripts.firecrawl_self_hosted_monitor.SelfHostedMonitor',Monitor)
    monkeypatch.setattr('scripts.firecrawl_self_hosted_monitor.time.sleep',lambda _:None)
    monkeypatch.setattr('utils.monitor_rendered_browser.render_page',lambda url,d:{'url':url,'accessible':True,
        'complete':True,'text':'','http_status':200,'visual_digest':'pixels','context_digest':'empty','image_review_required':True})
    config={'monitors':{'daily':{'targets':[{'urls':['https://test/image']}]}}}
    report=run_monitor(config,'daily',api_url='x',api_key='',state_dir=tmp_path,timeout=1,limit=None,
                       workers=2,send_notifications=False,scan_images=True)
    assert report['error']==0 and report['browser_coverage'][0]['complete']
    artifact=json.loads(next((tmp_path/'evidence').glob('*.json')).read_text())
    assert artifact['browser_fallback'] and artifact['markdown']=='' and len(artifact['failed_attempts'])==2
    assert len(list((tmp_path/'events').glob('*.json')))==1


def test_image_retry_budget_and_deadline_do_not_need_model_call(tmp_path):
    browser={'url':'https://test','complete':True,'image_seconds_remaining':0,'image_tiles':{'a':[{'path':'missing'}],'b':[]}}
    # A zero budget must fail before reading or submitting model input.
    class Client:
        def create_json_response_with_images(self,*a,**k):raise AssertionError('no remaining budget')
    with pytest.raises(RuntimeError,match='image_stage_timeout'):
        blind_image_reads(browser,tmp_path,Client(),extraction_schema(SERVICE_NAMES))


def test_flag_only_change_and_independent_text_offer_preserve_protected_image_sibling(tmp_path):
    data=writer_state()
    sibling=deepcopy(data['offers'][0]);sibling.update(id=21,offer_raw_text='Other image offer $100')
    data['offers'].append(sibling)
    item=deepcopy(data['items'][0]);item.update(offer_item_id=31,offer_id=21,item_name='Other')
    data['items'].append(item)
    plan=rest_plan(data)
    plan['operations'].append({'table':'clinic_promotions','action':'update','id':10,'values':{'is_offer_extracted':False}})
    plan['image_policy']={'version':1,'depends_on_images':False,'hold_parent_fields':True}
    receipt=render_pending(plan,{'offer_ids':[21]},tmp_path)
    assert receipt['status']=='pending_sql',receipt
    sql=Path(receipt['sql_path']).read_text()
    # The protected sibling and its item are snapshotted before and compared after the batch.
    assert "v_protected := " in sql and "raise exception 'protected_row_changed'" in sql
    assert "promo_offer_master where id = any('{21}'::bigint[])" in sql
    assert "update public.promo_offer_master set discount_price = 80 where id = 20;" in sql
    assert "where id = 21" not in sql and "offer_item_id = 31" not in sql.split("set constraints all immediate;")[0].split("update public.promo_offer_items")[-1]


def test_404_requires_both_response_visible_page_and_saved_screenshot(tmp_path):
    from utils.monitor_rendered_browser import confirmed_not_found
    shot=tmp_path/'404.png';shot.write_bytes(b'png')
    browser={'http_status':404,'scroll_complete':True,'failures':[],'text':'404 page not found','screenshot_path':str(shot)}
    assert confirmed_not_found(browser)
    for changed in ({'http_status':200},{'text':'Specials unavailable'},{'text':'Botox special $10/unit. Call (404) 555-0123 to book.'},{'screenshot_path':'missing'},{'failures':['blocked']}):
        assert not confirmed_not_found({**browser,**changed})


@pytest.mark.parametrize('quote',["Must receive 50 or more units to qualify", "Minimum 50 units", "50+ units", "50 units minimum"])
def test_minimum_purchase_variants_stay_terms_not_delivered_quantity(quote):
    from utils.monitor_price_plan import _term_values
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
    from utils.monitor_price_extract import _visible_key
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


def test_source_savings_contradiction_is_reviewed_before_trigger_can_overwrite_it():
    offer=_offer(offer_raw_text='SAVE $250 Face + Neck Now $1,400 (Reg. $1,750)',
        discount_price=1400,regular_price=1750,discount_amount=250,
        items=[{'item_name':'CO2 laser','quantity':None,'unit_price':None}])
    graph=_graph();graph.update(promotions=[],offers=[],items=[],services=[])
    plan=build_plan(SOURCE,graph,_evidence([offer]),{})
    assert not plan['operations']
    assert any(r['reason']=='contradictory_discount_evidence' for r in plan['review'])


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


def test_both_blind_batches_keep_footer_context_without_cross_round_pixels(tmp_path):
    browser={'url':'https://test','complete':True,'image_tiles':{'a':[],'b':[]}}
    for round_name in ('a','b'):
        for index in range(9):
            path=tmp_path/f'{round_name}{index}.png';path.write_bytes(f'{round_name}{index}'.encode())
            browser['image_tiles'][round_name].append({'path':str(path)})
    class Client:
        inputs=[]
        def create_json_response_with_images(self,prompt,**kwargs):
            self.inputs.append([body.decode() for body,mime in kwargs['images']])
            return {'offers':[],'all_offers_extracted':True},{'model':'gemini-3.5-flash-lite','raw_response':{}}
    client=Client();blind_image_reads(browser,tmp_path,client,extraction_schema(SERVICE_NAMES))
    assert client.inputs==[['a7','a8','a0','a1','a2','a3','a4','a5'],['a7','a8','a6'],
                          ['b7','b8','b0','b1','b2','b3','b4','b5'],['b7','b8','b6']]


def test_unsplit_unit_pricing_tiers_cannot_be_written_as_generic_total():
    raw='Botox special: New patients $12/unit; Spa members $11/unit; regular $13/unit'
    offer=_offer(offer_raw_text=raw,discount_price=11,regular_price=13,price_model='total',
                 items=[{'item_name':'Botox','service_id':12,'quantity':None,'unit_type':'unit'}])
    plan=build_plan(SOURCE,_graph(),_evidence([offer]),{})
    assert plan['operations']==[] and plan['review'][-1]['reason']=='ambiguous_unit_price_context'
