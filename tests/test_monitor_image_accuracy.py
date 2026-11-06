"""Image-policy regressions: blind inputs, semantic disagreement and final DML boundary."""
from copy import deepcopy
from unittest.mock import MagicMock, patch

import pytest

from utils.monitor_image_evidence import compare_reads, blind_image_reads, pixel_digest, tile_ranges
from utils.monitor_image_gate import image_write_block
from utils.monitor_price_extract import extraction_schema, extract_evidence
from utils.offer_extraction_llm import GeminiNativeClient
from monitor_price_plan_support import SOURCE, _graph, _offer
from utils.monitor_price_plan import build_plan
from monitor_image_support import SERVICE_NAMES, image_offer, reads


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
    with patch.object(client.session,'post',return_value=response):
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


def test_image_retry_budget_and_deadline_do_not_need_model_call(tmp_path):
    browser={'url':'https://test','complete':True,'image_seconds_remaining':0,'image_tiles':{'a':[{'path':'missing'}],'b':[]}}
    # A zero budget must fail before reading or submitting model input.
    class Client:
        def create_json_response_with_images(self,*a,**k):raise AssertionError('no remaining budget')
    with pytest.raises(RuntimeError,match='image_stage_timeout'):
        blind_image_reads(browser,tmp_path,Client(),extraction_schema(SERVICE_NAMES))


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
