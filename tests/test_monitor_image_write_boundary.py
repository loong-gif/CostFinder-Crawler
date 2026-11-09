from copy import deepcopy
import json
from pathlib import Path

from utils.monitor_image_gate import image_write_block
from utils.monitor_price_store import atomic_json, now
from utils.monitor_price_extract import extract_evidence
from utils.monitor_pending_sql import render_pending
from utils.monitor_price_writer import render_apply
from monitor_price_plan_support import SOURCE, _graph, _offer, _evidence
from utils.monitor_price_plan import build_plan
from monitor_image_support import SERVICE_NAMES, image_offer, proof_plan
from test_monitor_price_writer import plan_for as rest_plan, state as writer_state


def test_shadow_keeps_parent_fields_and_image_siblings_while_text_price_updates():
    graph=_graph();image=deepcopy(graph['offers'][0]);image.update(id=99,offer_raw_text='Dysport $12/unit')
    graph['offers'].append(image)
    graph['items'].append({**graph['items'][0],'offer_item_id':98,'offer_id':99,'item_name':'Dysport','service_id':97})
    graph['services'].append({'service_id':97,'business_id':7,'service_name':'Dysport','service_area':None})
    raw=_offer(discount_price=400,offer_raw_text=_offer()['offer_raw_text'].replace('$350','$400'))
    evidence=_evidence([raw],complete=False,image_policy={'version':1,'depends_on_images':False,'hold_parent_fields':True})
    plan=build_plan(SOURCE,graph,evidence,{})
    assert plan['status']=='ready'
    assert not any(op['table']=='clinic_promotions' for op in plan['operations'])
    sql=render_apply(plan,{})
    updates=[line for line in sql.splitlines() if line.strip().startswith('update public.')]
    assert updates and not any('where id = 99' in line or 'offer_item_id = 98' in line for line in updates)
    # The image sibling is version-checked after the writes instead of being touched.
    assert "(99, " in next(l for l in sql.splitlines() if 'unexpected_row_or_trigger_change: promo_offer_master' in l)
    assert next(op for op in plan['operations'] if op['table']=='promo_offer_master')['values']['discount_price']==400


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


def test_image_writes_are_on_by_default_and_only_an_explicit_switch_off_stops_them(tmp_path):
    plan=rest_plan()
    plan['image_policy']={'depends_on_images':True,'dual_read_verified':True}
    # 没有控制文件：图片 offer 照常进入双读核验，不因开关停下
    assert render_pending(plan,{},tmp_path)['category']=='image_dual_read_required'
    atomic_json(tmp_path/'price_sync_control.json',{'image_write_enabled':False})
    receipt=render_pending(plan,{},tmp_path)
    assert receipt['category']=='image_writes_disabled' and receipt['status']=='review'
    assert not (tmp_path/'pending_sql').exists()


def test_text_plan_renders_pending_sql_without_any_control_file(tmp_path):
    plan=rest_plan();plan['image_policy']={'depends_on_images':False};plan['event_id']='first'
    receipt=render_pending(plan,{},tmp_path)
    assert receipt['status']=='pending_sql' and Path(receipt['sql_path']).is_file()


def test_hidden_iframe_skips_subtree_and_query_failure_stays_partial():
    from utils.monitor_rendered_cdp import frame_regions
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
    assert parent['action']=='insert' and 'is_offer_extracted' not in parent['values']
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


def test_independent_text_offer_preserves_protected_image_sibling(tmp_path):
    data=writer_state()
    sibling=deepcopy(data['offers'][0]);sibling.update(id=21,offer_raw_text='Other image offer $100')
    data['offers'].append(sibling)
    item=deepcopy(data['items'][0]);item.update(offer_item_id=31,offer_id=21,item_name='Other')
    data['items'].append(item)
    plan=rest_plan(data)
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
    from utils.monitor_rendered_checks import confirmed_not_found
    shot=tmp_path/'404.png';shot.write_bytes(b'png')
    browser={'http_status':404,'scroll_complete':True,'failures':[],'text':'404 page not found','screenshot_path':str(shot)}
    assert confirmed_not_found(browser)
    for changed in ({'http_status':200},{'text':'Specials unavailable'},{'text':'Botox special $10/unit. Call (404) 555-0123 to book.'},{'screenshot_path':'missing'},{'failures':['blocked']}):
        assert not confirmed_not_found({**browser,**changed})
