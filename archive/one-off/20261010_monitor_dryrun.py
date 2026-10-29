#!/usr/bin/env python3
"""Read saved evidence and MCP SELECT results; produce a local dry-run only."""
import csv, json, re, sys, unicodedata
from collections import Counter, defaultdict
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation
from pathlib import Path
from copy import deepcopy
from urllib.parse import urlsplit

ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT))
from scripts.run_monitor_price_sync import bind_source
from utils.monitor_price_plan import _validated_offer, _resolve_items, _protected, _blanket
from utils.monitor_price_store import manual_protections
from utils.monitor_price_writer import normalize_source_url
from utils.offer_evidence_validator import validate_offer_evidence

OUT=ROOT/'output/monitor_price_sync/dryrun_20261010'
TODAY='2026-10-10'
FIELDS=['row_id','business_id','business_name','url','promotion_id','offer_id','item_id','table','field','old_value','proposed_value','action','certainty','reason','evidence_quote','captured_at','database_read_at','protection_status','execution_blockers','evidence_path','support_path','comparison_scope','evidence_kind']

def load(path):return json.loads(path.read_text())
def norm(value):
    s=unicodedata.normalize('NFKC',str(value or '')).casefold().replace('&',' and ')
    return re.sub(r'[^\w]+',' ',s).strip()
def clean(md):
    s=re.sub(r'data:image/[^\s)]+','',md or '')
    s=re.sub(r'!?\[([^\]]*)\]\([^\n]*?\)',r'\1',s)
    return re.sub(r'(?m)^\s*#+\s*|[*_`~]','',s)
def same(a,b):
    if a is None or b is None:return a is b
    if isinstance(a,list) or isinstance(b,list):return a==b
    if isinstance(a,bool) or isinstance(b,bool):return a==b
    try:return Decimal(str(a))==Decimal(str(b))
    except Exception:return norm(a)==norm(b)
def wire(x):return json.dumps(x,ensure_ascii=False) if isinstance(x,(list,dict,bool)) or x is None else str(x)
def iso(d):return datetime.fromtimestamp(d,timezone.utc).isoformat()
def identity(s):return norm(re.sub(r'\$\s*\d[\d,]*(?:\.\d+)?|\b\d+(?:\.\d+)?\s*%',' ',s or ''))
def price_facts(text):
    return {('money',format(Decimal(x.replace(',','')).normalize(),'f')) for x in re.findall(r'\$\s*(\d[\d,]*(?:\.\d+)?)',text)} | {('percent',format(Decimal(x).normalize(),'f')) for x in re.findall(r'\b(\d+(?:\.\d+)?)\s*%',text)}
def numeric(value):
    if value is None or isinstance(value,bool):return None
    try:
        d=Decimal(str(value))
        return d if d.is_finite() and d>=0 else None
    except (InvalidOperation,ValueError):return None
def literal_price_supported(field,value,quote):
    number=numeric(value)
    if number is None:return False
    if field=='discount_percent':return ('percent',format(number.normalize(),'f')) in price_facts(quote)
    for m in re.finditer(r'\$\s*(\d[\d,]*(?:\.\d+)?)',quote):
        if Decimal(m[1].replace(',',''))!=number:continue
        before=quote[max(0,m.start()-45):m.start()].lower();after=quote[m.end():m.end()+30].lower()
        regular=bool(re.search(r'(?:reg(?:ular)?\.?\s*(?:price)?\s*:?|usually|normally|original(?: price)?|was)\s*$',before))
        regular=regular or len(re.findall(r'~+',quote[:m.start()]))%2==1
        saving=bool(re.search(r'(?:save|savings? of|savings?|off|extra)\s*$',before) or re.match(r'\s*(?:off|savings?|saving)\b',after))
        if field=='regular_price' and regular:return True
        if field=='discount_amount' and saving and not re.search(r'extra\s*$',before):return True
        if field=='discount_price' and not regular and not saving:return True
    return False
def grounded_prices(raw, quote, resolved):
    """Report literal prices even when full write validation rejects scope.

    This produces no write plan. Unsupported quantities, original prices and
    missing service mappings remain blockers; absent values are never cleared.
    """
    master={k:None for k in ['regular_price','discount_price','discount_percent','discount_amount']}
    facts=price_facts(quote); flags=[]
    for field in master:
        value=raw.get(field)
        number=numeric(value)
        if literal_price_supported(field,value,quote):
            master[field]=value
        elif value is not None:flags.append('unsupported_'+field)
    if not any(master[k] is not None for k in ['discount_price','discount_percent','discount_amount']):return None,None
    unit=bool(re.search(r'/\s*(?:unit|syringe|session)|per\s+(?:unit|syringe|session)',quote,re.I))
    starting=bool(re.search(r'(?:from|starting at)\s*\$',quote,re.I))
    model='per_unit' if unit else 'from' if starting else 'total'
    if raw.get('price_model')!=model:flags.append('price_model_inference_requires_review')
    kind=raw.get('quantity_kind','fixed');minimum=bool(re.search(r'minimum|at least|\d+\s*\+|\d+\s+or more|must receive|first\s+\d+\s+units|up to',quote,re.I))
    if re.search(r'\bmonthly\b|per month|per year|annually',quote,re.I):flags.append('billing_period_requires_review')
    for original,want in zip(raw.get('items',[]),resolved):
        q=numeric(want.get('delivered_units'))
        if want.get('delivered_units') is not None and (q is None or q<=0 or q!=q.to_integral_value()):
            flags.append('invalid_fixed_quantity');q=None;want['delivered_units']=None
        fixed=bool(q is not None and re.search(r'\b'+re.escape(str(int(q)))+r'\s*(?:units?|sessions?|syringes?|treatments?|vials?)\b',quote,re.I))
        if minimum or kind!='fixed' or not fixed:
            if q is not None:flags.append('quantity_not_explicit')
            want['delivered_units']=None
        want['unit_price']=None
    package=bool(raw.get('is_package'))
    if model=='per_unit' and not package and len(resolved)==1:
        q=resolved[0]['delivered_units']
        if master['discount_price'] is not None:
            resolved[0]['unit_price']=master['discount_price']
            if q is not None:
                for field in ['discount_price','regular_price','discount_amount']:
                    if master[field] is not None:master[field]=float(Decimal(str(master[field]))*Decimal(str(q)))
    master.update(offer_raw_text=quote,price_model=model,is_package=package,is_active=True,
                  is_membership_required=bool(raw.get('is_membership_required')),
                  is_new_customer_required=bool(raw.get('is_new_customer_required')),conditions=[])
    for c in raw.get('conditions') or []:
        val=c.get('requirement') if isinstance(c,dict) else c
        evidence=c.get('evidence') if isinstance(c,dict) else c
        if val and evidence and norm(evidence) in norm(quote):master['conditions'].append(val)
        else:flags.append('condition_scope_requires_review')
    if master['regular_price'] is not None and master['discount_price'] is not None and model=='total':
        regular=Decimal(str(master['regular_price']));sale=Decimal(str(master['discount_price']))
        if regular>0 and sale<=regular:
            if master['discount_amount'] is None:master['discount_amount']=float(regular-sale);flags.append('discount_amount_derived_from_explicit_prices')
            if master['discount_percent'] is None:master['discount_percent']=float(((regular-sale)/regular*100).quantize(Decimal('0.01')));flags.append('discount_percent_derived_from_explicit_prices')
    return master,';'.join(sorted(set(flags)))
def campaign_time_issue(title,url):
    title=norm(title)
    if re.search(r'\b(?:november|december|january|february|march|april|may|june|july|august|september)\b',title) or re.search(r'mother.?s day|valentine|black friday',title):return 'seasonal_campaign_validity_requires_review'
    return ''
def cross_source_matches(quote,offers):
    key=identity(quote);result=[]
    for o in offers:
        old=identity(o.get('offer_raw_text'))
        if key==old or (len(old)>20 and old in key and price_facts(o.get('offer_raw_text','')) & price_facts(quote)):
            result.append(o)
    return result
def explicit_date(value,text):
    if not value:return ''
    y,m,d=map(int,value.split('-'));month=datetime(y,m,d).strftime('%B')
    patterns=[re.escape(value),rf'\b{month}\s+{d}(?:st|nd|rd|th)?\s*,?\s*{y}\b',rf'\b{m}/{d}/{y}\b',rf'\b{month}\s+\d{{1,2}}\s*[-–]\s*{d}\s*,?\s*{y}\b']
    for pat in patterns:
        match=re.search(pat,text,re.I)
        if match:
            context=text[max(0,match.start()-100):match.end()+100]
            if re.search(r'valid (?:through|until)|offer.{0,30}(?:ends|expires)|(?:ends|expires|ending)\s*(?:on)?|available.{0,30}(?:until|through)|sale.{0,30}(?:until|through)',context,re.I):return context
    return ''
def gender(s):
    w=bool(re.search(r'\bwomen\b|\bwomens\b|\bfemale\b',norm(s)))
    m=bool(re.search(r'\bmen\b|\bmens\b|\bmale\b',norm(s)))
    return 'women' if w and not m else 'men' if m and not w else None
def service_family(text):
    s=norm(text)
    if re.search(r'botox|dysport|xeomin|letybo|jeuveau|daxxify|neurotoxin',s):return 'neurotoxin'
    if re.search(r'hair removal|\blhr\b',s):return 'hair_removal'
    return ''

def main():
    db=load(OUT/'database_snapshot.json');db['memberships']=load(OUT/'membership_source_ownership.json')
    related=load(OUT/'related_source_context.json');db['related_source_context']=related
    packet=load(OUT/'server_evidence_packet.json');files=packet['files'];params=load(ROOT/'output/firecrawl_comparison_20261010/parameters.json');urls=params['urls']
    supplements={r['url']:r for r in load(ROOT/'output/firecrawl_comparison_20261010/v2_thinkbook.json')}
    stamp_file=ROOT/'output/firecrawl_comparison_20261010/v2_thinkbook.json'
    supplement_time=iso(stamp_file.stat().st_mtime)+' (batch completion; exact page time unavailable)'
    businesses={b['business_id']:b for b in db['businesses']};services={s['service_id']:s for s in db['services']}
    by_p=defaultdict(list);by_o=defaultdict(list);promos=defaultdict(list)
    for p in db['promotions']:promos[normalize_source_url(p['source_url'])].append(p)
    for o in db['offers']:by_p[o['promotion_id']].append(o)
    for i in db['items']:by_o[i['offer_id']].append(i)
    protection=manual_protections(db,files.get('manual_protections.json'));protected=_protected(protection)
    states={}
    for name,j in files.items():
        if name.startswith('daily_'):
            for u,s in j.get('pages',{}).items():states[u]=(s,j['updated_at'])
    failures={r['url']:r['error'] for x in files['monitor_run_report.json']['reports'] for r in x.get('page_errors',[])}
    candidates=defaultdict(list)
    for n,e in files.items():
        if n.startswith('sync/') and n.endswith('/evidence.json'):
            event_id=n.split('/')[1];plan=files.get(f'sync/{event_id}/plan.json',{});event=files.get(f'events/{event_id}.json',{})
            candidates[normalize_source_url(e['url'])].append({'evidence':e,'name':n,'prepared_at':plan.get('prepared_at',''),'event':event,'browser':files.get(f'sync/{event_id}/browser.json',{}),'ocr':files.get(f'sync/{event_id}/ocr.json',{})})
    rows=[];coverage=[];matched=set();compared_fields=defaultdict(set);dedup=set();resolved_pages=set()
    inventory={k:[] for k in ['promotions','offers','items']}
    def protected_for(p=None,o=None,i=None,parent=False):
        hits=[]
        if p and p['promotion_id'] in protected['clinic_promotions']:hits.append('promotion:'+str(p['promotion_id']))
        if o and o['id'] in protected['promo_offer_master']:hits.append('offer:'+str(o['id']))
        if i and i['offer_item_id'] in protected['promo_offer_items']:hits.append('item:'+str(i['offer_item_id']))
        if o:
            for child_item in by_o[o['id']]:
                if child_item['offer_item_id'] in protected['promo_offer_items']:hits.append('descendant_item:'+str(child_item['offer_item_id']))
        if parent and p:
            for child in by_p[p['promotion_id']]:
                if child['id'] in protected['promo_offer_master']:hits.append('descendant_offer:'+str(child['id']))
                for child_item in by_o[child['id']]:
                    if child_item['offer_item_id'] in protected['promo_offer_items']:hits.append('descendant_item:'+str(child_item['offer_item_id']))
        return ';'.join(hits) or 'none'
    def add(ctx,table,field,old,new,action,reason,quote='',p=None,o=None,i=None,blockers='',certainty=None,scope='field'):
        key=(ctx['key'],table,p['promotion_id'] if p else '',o['id'] if o else '',i['offer_item_id'] if i else '',field,wire(new),action)
        if key in dedup:return
        dedup.add(key);protect=protected_for(p,o,i,parent=table=='clinic_promotions')
        if protect!='none':blockers=';'.join(filter(None,[blockers,'manual_protection']))
        rows.append(dict(zip(FIELDS,['DR'+str(len(rows)+1).zfill(5),ctx.get('business_id',''),ctx.get('business_name',''),ctx['url'],p['promotion_id'] if p else '',o['id'] if o else '',i['offer_item_id'] if i else '',table,field,old,new,action,certainty or ('confirmed' if action in ['需更新','建议停用','可新增'] else 'review' if action=='待核验' else 'limited_match'),reason,quote,ctx['captured_at'],db['read_finished_at'],protect,blockers,ctx['evidence_path'],ctx.get('support_path',''),scope,ctx.get('evidence_kind','latest_wsl_attempt')])))
    for url in urls:
        key=normalize_source_url(url);ps=promos[key];s,state_time=states.get(url,({},''));artname='evidence/'+s.get('artifact_id','')+'.json';a=files.get(artname,{})
        primary_md=clean(a.get('markdown',''));status=((a.get('raw_response') or {}).get('data') or {}).get('metadata',{}).get('statusCode')
        failed=url in failures or not a or not primary_md.strip() or (status is not None and status>=400)
        ctx={'url':url,'key':key,'captured_at':a.get('captured_at') if not failed else state_time+' (latest attempt; successful capture unavailable)','evidence_path':str(OUT/'server_evidence'/artname) if a else str(OUT/'server_evidence/monitor_run_report.json'),'support_path':'','evidence_kind':'latest_wsl_markdown' if not failed else 'latest_wsl_attempt'}
        suppl=supplements.get(url,{});sd=(suppl.get('payload') or {}).get('data') or {};sm=clean(sd.get('markdown',''));ss=(sd.get('metadata') or {}).get('statusCode');sup_ok=bool(suppl.get('status')==200 and (suppl.get('payload') or {}).get('success') and sm.strip() and (ss is None or ss<400))
        source_text=primary_md if not failed else ''
        try:
            bid=bind_source(url,db)['business_id'];ctx.update(business_id=bid,business_name=businesses[bid]['name'])
            binding_error=''
        except RuntimeError as exc:binding_error=str(exc);bid=None
        except Exception as exc:binding_error=type(exc).__name__;bid=None
        cs=sorted(candidates[key],key=lambda e:e['prepared_at'],reverse=True);candidate=cs[0] if cs else None;e=candidate['evidence'] if candidate else {};browser=candidate['browser'] if candidate else {};ocr=candidate['ocr'] if candidate else {}
        if candidate:ctx['support_path']=str(OUT/'server_evidence'/candidate['name'])
        support_time=candidate['prepared_at'] if candidate else ''
        fresh_browser=bool(candidate and urlsplit(browser.get('final_url',url)).hostname and urlsplit(browser.get('final_url',url)).hostname.lower().removeprefix('www.')==urlsplit(url).hostname.lower().removeprefix('www.') and support_time[:10]==TODAY and browser.get('accessible') is True and (browser.get('http_status') or 200)<400 and normalize_source_url(browser.get('url',url))==key)
        support_text=clean(browser.get('text','')) if fresh_browser else ''
        ocr_text=ocr.get('text','') if fresh_browser and ocr.get('readable') is True else ''
        evidence_contexts={'latest_wsl_markdown':deepcopy(ctx),'existing_thinkbook_markdown':dict(ctx,captured_at=supplement_time,evidence_path=str(stamp_file),evidence_kind='existing_thinkbook_markdown')}
        if candidate:
            folder=OUT/'server_evidence'/candidate['name'].rsplit('/',1)[0]
            for kind,filename in [('existing_today_browser','browser.json'),('existing_today_ocr','ocr.json')]:
                evidence_contexts[kind]=dict(ctx,captured_at=support_time+' (processing time; exact capture time unavailable)',evidence_path=str(folder/filename),evidence_kind=kind)
        page={'url':url,'canonical_url':key,'primary_status':'failed_or_empty' if failed else 'available','primary_captured_at':a.get('captured_at'),'latest_attempt_at':state_time,'primary_path':ctx['evidence_path'],'primary_error':failures.get(url) or ('http_'+str(status) if status and status>=400 else None),'supplement_available':sup_ok,'supplement_batch_completed_at':supplement_time,'business_id':bid,'promotion_ids':[p['promotion_id'] for p in ps],'offer_ids':[o['id'] for p in ps for o in by_p[p['promotion_id']]],'item_ids':[i['offer_item_id'] for p in ps for o in by_p[p['promotion_id']] for i in by_o[o['id']]],'extraction_prepared_at':support_time,'extraction_path':ctx['support_path'],'verified_offer_count':0,'matched_offer_count':0,'issues':[]}
        start=len(rows)
        if failed:
            add(ctx,'source','page_evidence',failures.get(url) or ('http_'+str(status)),None,'待核验','最新 WSL 抓取失败、空正文或错误页；不以旧基线认定优惠消失。'+('已有 Thinkbook 正文，仅作补充。' if sup_ok else ''),p=ps[0] if len(ps)==1 else None)
            page['issues'].append('primary_failed')
        if binding_error:
            add(ctx,'source','business_match',[p['business_id'] for p in ps],None,'待核验',binding_error);page['issues'].append(binding_error)
        if len(ps)>1:
            add(ctx,'clinic_promotions','source_match',[p['promotion_id'] for p in ps],None,'待核验','同一规范化 URL 对应多条活动；逐条保留，不凭 URL 选择覆盖目标。');page['issues'].append('multiple_promotions')
        expected=[]
        for raw in e.get('offers',[]):
            quote=raw.get('offer_raw_text','').strip()
            if not quote:continue
            if re.search(r'(?:/\s*month|per month|monthly|sign.?up fee)',quote,re.I) and re.search(r'member|sign.?up',quote,re.I):
                page['excluded_membership_fee_count']=page.get('excluded_membership_fee_count',0)+1
                continue
            nq=norm(clean(quote));where=''
            if nq and nq in norm(source_text):where='latest_wsl_markdown'
            elif nq and sup_ok and nq in norm(sm):where='existing_thinkbook_markdown'
            elif nq and nq in norm(support_text):where='existing_today_browser'
            elif nq and nq in norm(ocr_text):where='existing_today_ocr'
            if not where:
                add(ctx,'promo_offer_master','offer_evidence',None,{'offer_raw_text':quote,'price_model':raw.get('price_model'),'discount_price':raw.get('discount_price')},'待核验','已有提取结果无法在本次有效正文或已保存当日可读截图证据中定位，不能直接作为当前报价。',quote,p=ps[0] if len(ps)==1 else None,blockers='unresolved_image_or_visibility',scope='candidate_offer');continue
            text={'latest_wsl_markdown':source_text,'existing_thinkbook_markdown':sm,'existing_today_browser':support_text,'existing_today_ocr':ocr_text}[where]
            resolved=[];mapping=''
            try:resolved=_resolve_items(raw,[v for v in db['services'] if v['business_id']==bid],bid)
            except Exception as exc:
                mapping=str(exc)
                for it in raw.get('items',[]):
                    quantity=it.get('delivered_units',it.get('quantity')) if raw.get('quantity_kind','fixed')=='fixed' else None
                    resolved.append({'item_name':it.get('item_name') or it.get('service_name'),'service_id':None,'delivered_units':quantity,'unit_price':None})
            try:master,reason=_validated_offer(raw,text,resolved)
            except Exception as exc:master=None;reason=str(exc)
            if master is None:
                fallback,price_blockers=grounded_prices(raw,quote,resolved)
                promo_context=bool(re.search(r'(?:specials|special-offers|promos|promotions|summer-promo|fall-promo|new-patient-special)',url,re.I) or re.search(r'\boff\b|\bsave\b|\bfree\b|\bspecial\b',quote,re.I))
                if fallback and promo_context:
                    master=fallback;mapping=';'.join(filter(None,[mapping,reason,price_blockers]))
            if master is None:
                add(ctx,'promo_offer_master','offer_validation',None,quote,'待核验',reason or 'invalid_evidence',quote,p=ps[0] if len(ps)==1 else None,blockers='evidence_validation');continue
            if raw.get('price_model')=='per_unit' and raw.get('quantity_kind')=='minimum':
                assert all(it['delivered_units'] is None for it in resolved)
            if master['is_package']:assert all(it['unit_price'] is None for it in resolved)
            expected.append({'raw':raw,'master':master,'items':resolved,'quote':quote,'where':where,'text':text,'mapping':mapping,'context':evidence_contexts[where]})
        page['verified_offer_count']=len(expected)
        page['verified_claims']=[{'quote':v['quote'],'evidence_kind':v['where'],'path':v['context']['evidence_path'],'captured_at':v['context']['captured_at']} for v in expected]
        all_offers=[o for p in ps for o in by_p[p['promotion_id']]]
        page_context=ctx
        for v in expected:
            ctx=v['context']
            raw,master,desired,quote=v['raw'],v['master'],v['items'],v['quote']
            quals=(bool(master.get('is_membership_required')),bool(master.get('is_new_customer_required')))
            matches=[o for o in all_offers if identity(o['offer_raw_text'])==identity(quote)]
            if not matches:
                # Exact service composition and qualification; generic service alone is insufficient.
                ds={i['service_id'] for i in desired if i['service_id'] is not None}
                if ds and len(ds)==len(desired):
                    matches=[o for o in all_offers if {i['service_id'] for i in by_o[o['id']]}==ds and (bool(o.get('is_membership_required')),bool(o.get('is_new_customer_required')))==quals and (len(all_offers)==1 or norm(raw.get('items',[{}])[0].get('service_area')) and norm(raw['items'][0]['service_area']) in norm(o['offer_raw_text']))]
            # Require all explicitly named item scopes in the old offer text; preserve gender context.
            if not matches and desired:
                names=[norm(it.get('item_name') or it.get('service_name')) for it in raw['items']]
                names=[n for n in names if n and n not in {'others','laser hair removal','facial','tox','any service','face','neck','body'}]
                if len(names)==len(desired):
                    textnorm=norm(v['text']);pos=textnorm.find(norm(clean(quote)));prefix=textnorm[:pos] if pos>=0 else '';lastw=prefix.rfind('for women');lastm=prefix.rfind('for men');context_gender='women' if lastw>lastm and lastw>=0 else 'men' if lastm>lastw and lastm>=0 else None
                    matches=[o for o in all_offers if all(n in norm(o['offer_raw_text']) for n in names) and (not context_gender or not gender(o['offer_raw_text']) or gender(o['offer_raw_text'])==context_gender)]
            family=service_family(quote+' '+ ' '.join(str(it.get('service_name') or '') for it in raw.get('items',[])))
            matches=[o for o in matches if identity(o['offer_raw_text'])==identity(quote) or not family or not service_family(o['offer_raw_text']+' '+ ' '.join(str(services.get(it['service_id'],{}).get('service_name','')) for it in by_o[o['id']])) or family==service_family(o['offer_raw_text']+' '+ ' '.join(str(services.get(it['service_id'],{}).get('service_name','')) for it in by_o[o['id']]))]
            # Prefer the sole active match, but never collapse duplicate active records.
            active=[o for o in matches if o['is_active']]
            if active:matches=active
            if len(matches)!=1:
                page['issues'].append('offer_match_requires_review')
                other_source=[o for o in db['offers']+related['offers'] if o['business_id']==bid and o['promotion_id'] not in {p['promotion_id'] for p in ps}]
                related_matches=cross_source_matches(quote,other_source)
                action='待核验' if matches or ps or binding_error else '可新增'
                time_issue=campaign_time_issue(e.get('promotion_title'),url)
                if related_matches or time_issue or 'billing_period_requires_review' in v['mapping'] or (e.get('campaign_end_date') and e['campaign_end_date']<TODAY) or (e.get('campaign_start_date') and e['campaign_start_date']>TODAY):action='待核验'
                reason='现有优惠匹配不唯一，不能选择覆盖 ID。' if matches else '已有来源但未找到唯一对应优惠；新增还是替换旧优惠需复核。' if ps else '有效原文提供了优惠，但对应活动/优惠尚不存在。'
                if related_matches:reason='同商家其他来源已有相同条款；需确认来源归属，避免重复新增。'
                blockers=';'.join(filter(None,[v['mapping'],'offer_match_ambiguous' if matches or ps else binding_error,'existing_other_source_offer' if related_matches else '',time_issue]))
                if time_issue:reason+=' 页面具有季节/月份含义，未证明本次活动有效期。'
                if not ps:blockers=';'.join(filter(None,[blockers,'new_promotion_requires_source_ownership_review']))
                if action=='待核验':master=dict(master,is_active=None)
                add(ctx,'promo_offer_master','candidate_offer',[o['id'] for o in matches or related_matches] if matches or related_matches else None,{'master':master,'items':desired},action,reason,quote,p=ps[0] if len(ps)==1 else None,blockers=blockers,scope='candidate_offer');continue
            old=matches[0];parent=next(p for p in ps if p['promotion_id']==old['promotion_id']);matched.add(old['id']);page['matched_offer_count']+=1
            blockers=[]
            if v['mapping']:blockers.append(v['mapping'])
            if not old['is_active'] or not parent['is_active']:blockers.append('historical_record_requires_review')
            if parent.get('campaign_end_date') and parent['campaign_end_date']<TODAY:blockers.append('historical_campaign_or_date_requires_review')
            if old.get('price_model')!=master['price_model'] or bool(old.get('is_package'))!=master['is_package']:blockers.append('price_basis_or_package_changed')
            if any(old.get(k) is True and master.get(k) is not True for k in ['is_membership_required','is_new_customer_required']):blockers.append('qualification_scope_changed_or_unproven')
            scope_pattern=r'\b(?:buy\s+\d+|\d+\s*(?:units?|sessions?|syringes?|vials?|treatments?))\b'
            if set(re.findall(scope_pattern,norm(old['offer_raw_text'])))!=set(re.findall(scope_pattern,norm(quote))):blockers.append('fixed_quantity_or_package_scope_changed')
            if norm(clean(old['offer_raw_text']))!=norm(clean(quote)):
                add(ctx,'promo_offer_master','offer_raw_text',old['offer_raw_text'],quote,'待核验','当前条款原文与数据库原文不同；需保留活动、附赠、适用部位和资格上下文后确认是否覆盖同一优惠。',quote,p=parent,o=old,blockers=';'.join(blockers+['offer_scope_and_wording']))
            # New values are asserted only for supported fields; absence cannot clear a prior value.
            page['issues'].extend(b for b in blockers if b in ['historical_record_requires_review','price_basis_or_package_changed','fixed_quantity_or_package_scope_changed'])
            for field in ['discount_price','regular_price','discount_percent','discount_amount','price_model','is_package']:
                value=master.get(field);prior=old.get(field)
                if value is None:continue
                compared_fields[old['id']].add(field)
                if same(prior,value):continue
                action='待核验' if any(b in blockers for b in ['historical_record_requires_review','historical_campaign_or_date_requires_review','price_basis_or_package_changed','fixed_quantity_or_package_scope_changed','qualification_scope_changed_or_unproven']) else '需更新'
                note='唯一匹配优惠的已验证原文与当前数据库字段不同；不清空来源未提供的其他价格字段。'
                if field in ['discount_amount','discount_percent'] and 'derived_from_explicit_prices' in v['mapping']:note+=' 建议值由原文直接给出的同口径原价和优惠价计算。'
                if field=='discount_percent' and master.get('regular_price') and master.get('discount_price') is not None:
                    derived=(Decimal(str(master['regular_price']))-Decimal(str(master['discount_price'])))/Decimal(str(master['regular_price']))*100
                    if abs(derived-Decimal(str(value)))>Decimal('0.01'):
                        action='待核验';note+=' 网页折扣宣传数与明确原价/优惠价的计算值不一致，需保留金额并核对取整口径。'
                add(ctx,'promo_offer_master',field,prior,value,action,note,quote,p=parent,o=old,blockers=';'.join(blockers))
            for field,pat in [('is_new_customer_required',r'new (?:patient|client|customer)|first time|first visit'),('is_membership_required',r'members? only|members? price|membership required')]:
                if master.get(field) is True and re.search(pat,quote,re.I) and old.get(field) is not True:
                    add(ctx,'promo_offer_master',field,old.get(field),True,'需更新','原文明确要求资格，数据库未表达该要求。',quote,p=parent,o=old,blockers=';'.join(blockers));compared_fields[old['id']].add(field)
            old_cond=old.get('conditions');new_cond=master.get('conditions')
            if old_cond!=new_cond and new_cond:
                add(ctx,'promo_offer_master','conditions',old_cond,new_cond,'待核验','条件表达不同，保留 NULL 与空数组区别；需确认旧限制是否仍适用。',quote,p=parent,o=old,blockers='qualification_scope')
            if _blanket(quote):
                for item in by_o[old['id']]:
                    if item['service_id'] is not None:add(ctx,'promo_offer_items','service_id',item['service_id'],None,'需更新','Blanket offer 不应关联特定服务；原文证明适用范围。',quote,p=parent,o=old,i=item,blockers=';'.join(blockers))
            for want in desired:
                its=[it for it in by_o[old['id']] if (want.get('service_id') is not None and it['service_id']==want['service_id']) or norm(it.get('item_name'))==norm(want.get('item_name'))]
                if len(its)!=1:continue
                item=its[0]
                for field in ['unit_price','delivered_units']:
                    value=want.get(field)
                    if value is None:continue
                    if field=='delivered_units' and not re.search(r'\b\d+\s*(?:units?|sessions?|syringes?|treatments?|vials?)\b',quote,re.I):continue
                    if same(item.get(field),value):continue
                    if field=='unit_price' and old.get('price_model')!=master['price_model']:continue
                    add(ctx,'promo_offer_items',field,item.get(field),value,'待核验' if any(b in blockers for b in ['historical_record_requires_review','historical_campaign_or_date_requires_review','price_basis_or_package_changed','fixed_quantity_or_package_scope_changed']) else '需更新','按原文的固定交付数量/单价口径核对，最低门槛不当作固定数量，套餐不分摊。',quote,p=parent,o=old,i=item,blockers=';'.join(blockers))
        ctx=page_context
        # Promotion content uses confirmed fragments, never replaces unverified old clauses.
        for parent in ps:
            new_quotes=[v['quote'] for v in expected if price_facts(v['quote'])-price_facts(wire(parent.get('promotion_content')))]
            if new_quotes:
                fragment_context=next(v['context'] for v in expected if v['quote']==new_quotes[0])
                add(fragment_context,'clinic_promotions','promotion_content',parent.get('promotion_content'),{'verified_current_fragments':list(dict.fromkeys(new_quotes)),'replace_whole_content':False},'需更新' if len(ps)==1 and parent['is_active'] else '待核验','当前原文存在旧活动正文未表达的金额/折扣事实；只建议核对并补齐这些片段，不因措辞差异替换正文，也不删除未核验旧条款。',new_quotes[0],p=parent,blockers='partial_content_and_semantic_reconciliation',scope='partial_content')
            date_text=source_text+'\n'+support_text+'\n'+ocr_text
            for field in ['campaign_start_date','campaign_end_date']:
                desired_date=e.get(field);proof=explicit_date(desired_date,date_text)
                if desired_date and not same(parent.get(field),desired_date):
                    add(ctx,'clinic_promotions',field,parent.get(field),desired_date,'需更新' if proof and len(ps)==1 and parent['is_active'] else '待核验','提取的活动日期与数据库不同。'+('原文包含明确日期。' if proof else '未找到包含年份和边界日期的直接原文，不能采用推断日期。'),proof or '提取结果日期：'+desired_date,p=parent,blockers='' if proof and len(ps)==1 and parent['is_active'] else 'campaign_match_or_explicit_date')
            if parent['is_active'] and parent.get('campaign_end_date') and parent['campaign_end_date']<TODAY:
                proof=explicit_date(parent['campaign_end_date'],date_text)
                action='建议停用' if proof and not failed and len(ps)==1 else '待核验'
                add(ctx,'clinic_promotions','is_active',True,False,action,'数据库结束日期已过；'+('有效页面明确支持同一截止日期。' if proof and not failed else '需区分旧活动结束、当前新活动和数据库日期错误，不凭空正文或模型推断直接下架。'),proof or '数据库 campaign_end_date='+parent['campaign_end_date'],p=parent,blockers='' if action=='建议停用' else 'expiry_source_verification')
                if action=='建议停用':
                    for old in by_p[parent['promotion_id']]:
                        if old['is_active']:add(ctx,'promo_offer_master','is_active',True,False,'建议停用','上游活动明确结束，关联优惠需同时停用。',proof,p=parent,o=old)
            title=e.get('promotion_title')
            if title and norm(title)!=norm(parent.get('promotion_title')) and norm(title) in norm(date_text) and re.search(r'january|february|march|april|may|june|july|august|september|october|november|december|summer|fall|holiday',parent.get('promotion_title',''),re.I):
                add(ctx,'clinic_promotions','promotion_title',parent.get('promotion_title'),title,'待核验','网页当前标题与存储活动标题不同；需确认是否同一活动，而非覆盖历史活动。',title,p=parent,blockers='campaign_identity')
        # A complete fresh source can prove removal; incomplete sources only get review rows.
        complete=bool(fresh_browser and e.get('complete') is True and not failed and not page['issues'] and len(expected)==len(e.get('offers',[])))
        for parent in ps:
            for old in by_p[parent['promotion_id']]:
                if old['id'] in matched:
                    if not any(r['offer_id']==old['id'] and r['action'] in ['需更新','待核验','建议停用'] for r in rows[start:]):add(ctx,'promo_offer_master','verified_fields',{k:old.get(k) for k in sorted(compared_fields[old['id']])},None,'未发现差异','仅已核对价格/口径字段一致；未检查字段、图片或日期不据此声明一致。',old['offer_raw_text'],p=parent,o=old,scope='limited_fields')
                elif old['is_active']:
                    add(ctx,'promo_offer_master','is_active',True,False if complete else None,'建议停用' if complete else '待核验','完整当日证据未包含该旧优惠。' if complete else '未找到唯一可核实对应，可能为图片、资格、服务映射或活动差异；不能证明已移除。',e.get('no_current_offers_quote') or ('已核验完整页面优惠集合' if complete else old['offer_raw_text']),p=parent,o=old,blockers='' if complete else 'removal_not_proven')
        if len(rows)==start and not any(normalize_source_url(r['url'])==key for r in rows):
            add(ctx,'source','comparison_result',None,None,'待核验','现有证据不足以形成逐字段比较；不声明整页与数据库一致。',scope='page_summary')
        page['row_ids']=[r['row_id'] for r in rows if normalize_source_url(r['url'])==key]
        page['actions']=sorted({r['action'] for r in rows if normalize_source_url(r['url'])==key});coverage.append(page)
    # Multiple distinct source claims must not overwrite one existing offer.
    claim_sets=defaultdict(set)
    for r in rows:
        if r['offer_id'] and r['evidence_quote'] and r['field'] not in ['is_active','verified_fields']:claim_sets[r['offer_id']].add(identity(r['evidence_quote']))
    for r in rows:
        if r['offer_id'] and len(claim_sets[r['offer_id']])>1 and r['action'] in ['需更新','未发现差异']:
            r.update(action='待核验',certainty='review',execution_blockers=r['execution_blockers']+';multiple_claims_for_same_offer',reason=r['reason']+' 同一旧 ID 匹配多个不同条款，不能覆盖。')
    # These two URLs advertise the same mix-and-match bundle, not two new records.
    shared_bundle=[r for r in rows if r['field']=='candidate_offer' and r['action']=='可新增' and re.search(r'diamondglow',r['evidence_quote'],re.I) and re.search(r'microneedling',r['evidence_quote'],re.I) and re.search(r'4.?treatment',r['evidence_quote'],re.I)]
    for r in shared_bundle:
        others=[x for x in shared_bundle if x['business_id']==r['business_id'] and x['url']!=r['url'] and price_facts(x['evidence_quote'])==price_facts(r['evidence_quote'])]
        if others:
            r.update(action='待核验',certainty='review',execution_blockers=r['execution_blockers']+';cross_url_same_bundle',reason=r['reason']+' 同商家其他页面也展示同一四次混选套餐，需合并来源后确定一条新增建议。')
    for page in coverage:page['actions']=sorted({r['action'] for r in rows if normalize_source_url(r['url'])==page['canonical_url']})
    # Inventory records prove full database coverage, including preserved inactive history.
    for p in db['promotions']:inventory['promotions'].append({'id':p['promotion_id'],'source_url':p['source_url'],'is_active':p['is_active'],'result_row_ids':[r['row_id'] for r in rows if r['promotion_id']==p['promotion_id']]})
    for o in db['offers']:inventory['offers'].append({'id':o['id'],'promotion_id':o['promotion_id'],'is_active':o['is_active'],'verification':'matched_current_fields' if o['id'] in matched else 'review_current' if o['is_active'] else 'inactive_history_retained_not_reactivated','compared_fields':sorted(compared_fields[o['id']]),'result_row_ids':[r['row_id'] for r in rows if r['offer_id']==o['id']]})
    for i in db['items']:inventory['items'].append({'id':i['offer_item_id'],'offer_id':i['offer_id'],'result_row_ids':[r['row_id'] for r in rows if r['item_id']==i['offer_item_id']]})
    with (OUT/'dryrun_changes.csv').open('w',encoding='utf-8-sig',newline='') as f:
        w=csv.DictWriter(f,fieldnames=FIELDS);w.writeheader()
        for r in rows:w.writerow({k:wire(v) if isinstance(v,(dict,list,bool)) or v is None else v for k,v in r.items()})
    evidence={'generated_at':datetime.now(timezone.utc).isoformat(),'as_of_date':TODAY,'read_only':True,'database_snapshot':db,'database_transport':'Supabase MCP execute_sql SELECT only','database_inventory':inventory,'manual_protections':protection,'page_coverage':coverage,'changes':rows,'final_database_read_validation':load(OUT/'final_read_validation.json'),'server_read_validation':load(OUT/'server_read_validation.json'),'primary_evidence_root':str(OUT/'server_evidence'),'supplement_path':str(ROOT/'output/firecrawl_comparison_20261010/v2_thinkbook.json'),'notes':['No database writes, DDL, task invocation, baseline changes, new scrape, new OCR or LLM requests.','Whole-offer write validation is reused where possible. Independently grounded literal field differences remain visible with blockers when the full validator fails. Such rows do not authorize a write.','Inactive history is retained; price/expiry changes need campaign identity review.','Observed database scope was read in ordered MCP pages, not an atomic transaction; final ID/version reread showed no changes.','WSL uses PST/PDT. Its extraction observed_date 2026-10-09 corresponds to Oct 10 UTC/Asia-Shanghai processing; saved browser/OCR modification times were reread for provenance.']}
    (OUT/'dryrun_evidence.json').write_text(json.dumps(evidence,ensure_ascii=False,indent=2))
    summary={'pages':len(coverage),'canonical_sources':len({p['canonical_url'] for p in coverage}),'primary_available':sum(p['primary_status']=='available' for p in coverage),'rows':len(rows),'row_actions':dict(Counter(r['action'] for r in rows)),'matched_offers':len(matched),'db_counts':{k:len(db[k]) for k in ['promotions','offers','items','services','businesses']},'page_actions':{a:sum(a in p['actions'] for p in coverage) for a in ['需更新','建议停用','可新增','未发现差异','待核验']}}
    (OUT/'summary.json').write_text(json.dumps(summary,ensure_ascii=False,indent=2));print(json.dumps(summary,ensure_ascii=False,indent=2))

if __name__=='__main__':main()
