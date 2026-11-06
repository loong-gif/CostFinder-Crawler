from copy import deepcopy

from utils.monitor_image_evidence import critical_fields
from utils.monitor_price_store import atomic_json, now
from monitor_price_plan_support import _offer

SERVICE_NAMES = ['Botox', 'Dermal Filler', 'Others']


def image_offer():
    return {**_offer(),'terms_complete':True,'evidence_regions':[0],
            'campaign_start_date':None,'campaign_end_date':None,'campaign_title':None}


def reads(offer):
    return {'offers':[offer],'all_offers_extracted':True}


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
