#!/usr/bin/env python3
"""Verify this saved read-only batch; no network or database calls."""
import ast
import csv
import importlib.util
import json
import re
from collections import Counter
from datetime import datetime, timezone
from decimal import Decimal
from pathlib import Path

ROOT=Path(__file__).resolve().parents[1]
OUT=ROOT/'output/monitor_price_sync/dryrun_20261010'
spec=importlib.util.spec_from_file_location('dryrun',ROOT/'one-off/20261010_monitor_dryrun.py')
dryrun=importlib.util.module_from_spec(spec);spec.loader.exec_module(dryrun)

def read(path):return json.loads(path.read_text())
def check(name,condition,details):
    results.append({'check':name,'result':'PASS' if condition else 'FIX','details':details})
def evidence_text(path,kind,url):
    source=read(Path(path))
    if kind=='existing_thinkbook_markdown':
        source=next(x for x in source if x['url']==url)
        return dryrun.clean((source.get('payload',{}).get('data') or {}).get('markdown',''))
    if kind=='existing_today_browser':return dryrun.clean(source.get('text',''))
    if kind=='existing_today_ocr':return source.get('text','')
    return dryrun.clean(source.get('markdown',''))

results=[]
e=read(OUT/'dryrun_evidence.json');db=e['database_snapshot'];rows=e['changes'];pages=e['page_coverage']
urls=read(ROOT/'output/firecrawl_comparison_20261010/parameters.json')['urls']
check('189_page_coverage',len(pages)==189 and Counter(p['url'] for p in pages)==Counter(urls),{'configured':len(urls),'covered':len(pages),'canonical':len({p['canonical_url'] for p in pages})})
promos={p['promotion_id']:p for p in db['promotions']};offers={o['id']:o for o in db['offers']};items={i['offer_item_id']:i for i in db['items']};services={s['service_id']:s for s in db['services']}
check('database_inventory_complete',all({x['id'] for x in e['database_inventory'][k]}==set(records) for k,records in [('promotions',promos),('offers',offers),('items',items)]),{k:len(db[k]) for k in ['promotions','offers','items']})
errors=[]
for o in offers.values():
    if o['promotion_id'] not in promos or promos[o['promotion_id']]['business_id']!=o['business_id']:errors.append(['offer',o['id']])
for i in items.values():
    if i['offer_id'] not in offers:errors.append(['item',i['offer_item_id']])
    if i['service_id'] is not None and (i['service_id'] not in services or services[i['service_id']]['business_id']!=offers[i['offer_id']]['business_id']):errors.append(['service',i['offer_item_id']])
for r in rows:
    if r['promotion_id'] and r['promotion_id'] not in promos:errors.append(['row_promotion',r['row_id']])
    if r['offer_id'] and (r['offer_id'] not in offers or offers[r['offer_id']]['promotion_id']!=r['promotion_id']):errors.append(['row_offer',r['row_id']])
    if r['item_id'] and (r['item_id'] not in items or items[r['item_id']]['offer_id']!=r['offer_id']):errors.append(['row_item',r['row_id']])
check('all_associated_ids_traceable',not errors,errors)
final=read(OUT/'final_read_validation.json');current=final['results'][0][0]['snapshot_check'];changed=[]
for key,records in [('promotions',promos),('offers',offers),('items',items)]:
    latest={x['id']:x['updated_at'] for x in current[key]['versions']}
    if set(latest)!=set(records):changed.append([key,'id_set'])
    for ident,record in records.items():
        if ident not in latest or datetime.fromisoformat(record['updated_at'])!=datetime.fromisoformat(latest[ident]):changed.append([key,ident])
check('final_database_ids_and_versions_unchanged',not changed,{'reread_at':current['read_at'],'changed':changed})
with (OUT/'dryrun_changes.csv').open(encoding='utf-8-sig',newline='') as f:csvrows=list(csv.DictReader(f))
check('csv_matches_evidence_json',len(csvrows)==len(rows) and all(c=={k:dryrun.wire(v) if isinstance(v,(dict,list,bool)) or v is None else str(v) for k,v in r.items()} for c,r in zip(csvrows,rows)),{'csv_rows':len(csvrows),'unique_row_ids':len({r['row_id'] for r in rows}),'columns':len(dryrun.FIELDS)})

bad_quotes=[];bad_values=[]
for r in rows:
    if r['action'] not in ['需更新','可新增','建议停用']:continue
    text=evidence_text(r['evidence_path'],r['evidence_kind'],r['url'])
    if dryrun.norm(dryrun.clean(r['evidence_quote'])) not in dryrun.norm(text):bad_quotes.append(r['row_id'])
    if r['field']=='promotion_content':
        page=next(p for p in pages if p['url']==r['url'])
        for quote in r['proposed_value']['verified_current_fragments']:
            claim=next((c for c in page['verified_claims'] if c['quote']==quote),None)
            if not claim or dryrun.norm(dryrun.clean(quote)) not in dryrun.norm(evidence_text(claim['path'],claim['evidence_kind'],r['url'])):bad_quotes.append([r['row_id'],'fragment'])
        if r['proposed_value']['replace_whole_content'] is not False:bad_values.append([r['row_id'],'whole_content'])
    elif r['field'] in ['regular_price','discount_price','discount_amount','discount_percent']:
        value=r['proposed_value']
        if not dryrun.literal_price_supported(r['field'],value,r['evidence_quote']):
            m=list(__import__('re').finditer(r'\$\s*(\d[\d,]*(?:\.\d+)?)',r['evidence_quote']))
            prices=[Decimal(x[1].replace(',','')) for x in m]
            valid=len(prices)==2 and r['field'] in ['discount_amount','discount_percent'] and (prices[0]-prices[1] if r['field']=='discount_amount' else (prices[0]-prices[1])/prices[0]*100).quantize(Decimal('0.01'))==Decimal(str(value))
            if not valid:bad_values.append(r['row_id'])
    elif r['field']=='candidate_offer':
        for field in ['regular_price','discount_price','discount_amount','discount_percent']:
            value=r['proposed_value']['master'].get(field)
            if value is not None and not dryrun.literal_price_supported(field,value,r['evidence_quote']) and field+'_derived_from_explicit_prices' not in r['execution_blockers']:bad_values.append([r['row_id'],field])
check('all_confirmed_proposals_grounded_in_saved_source',not bad_quotes,{'checked_rows':sum(r['action'] in ['需更新','可新增','建议停用'] for r in rows),'bad_quotes':bad_quotes})
check('confirmed_price_values_have_literal_or_supported_arithmetic',not bad_values,bad_values)
check('no_new_database_ids_allocated',all(not r['offer_id'] and not r['item_id'] for r in rows if r['action']=='可新增'),{'new_candidates':sum(r['action']=='可新增' for r in rows)})
duplicates={p['canonical_url'] for p in pages if len(p['promotion_ids'])>1}
check('duplicate_url_promotions_preserved_and_not_overwritten',all(not (r['action'] in ['需更新','建议停用'] and dryrun.normalize_source_url(r['url']) in duplicates) for r in rows),{'canonical_sources_with_multiple_promotions':len(duplicates),'avere_promotions':next(p['promotion_ids'] for p in pages if 'averebeauty.com/summer-promo' in p['url'])})
check('inaccessible_or_incomplete_sources_do_not_prove_removal',not any(r['action']=='建议停用' for r in rows),{'confirmed_stop_rows':sum(r['action']=='建议停用' for r in rows),'failed_primary_pages':sum(p['primary_status']!='available' for p in pages)})
check('inactive_history_not_reactivated',not any(r['field']=='is_active' and r['proposed_value'] is True for r in rows),{'inactive_offers':sum(not o['is_active'] for o in offers.values())})
protected_rows=[r for r in rows if r['protection_status']!='none']
check('manual_protection_and_parent_effects_marked',all('manual_protection' in r['execution_blockers'] for r in protected_rows) and any(r['promotion_id']==2 and 'descendant_offer:8590' in r['protection_status'] for r in rows),{'marked_rows':len(protected_rows),'protected_ids':sorted({x['id'] for x in e['manual_protections']['entries']})})
check('blanket_associations_read_only_check',final['results'][1]==[],{'result_rows':len(final['results'][1]),'scope':'active offers linked to the monitored promotions'})
check('unchanged_results_are_field_limited',all(r['comparison_scope']=='limited_fields' and isinstance(r['old_value'],dict) and bool(r['old_value']) for r in rows if r['action']=='未发现差异'),{'limited_match_rows':sum(r['action']=='未发现差异' for r in rows)})

raw={'regular_price':13,'discount_price':10,'price_model':'per_unit','is_package':False,'quantity_kind':'minimum','items':[{}]}
desired=[{'delivered_units':50,'unit_price':None}];master,flags=dryrun.grounded_prices(raw,'$10/unit, regular $13/unit; minimum 50 units',desired)
check('minimum_is_not_fixed_delivery',master['discount_price']==10 and desired[0]['delivered_units'] is None and desired[0]['unit_price']==10,{'master_price':master['discount_price'],'item':desired[0]})
raw=dict(raw,quantity_kind='fixed');desired=[{'delivered_units':20,'unit_price':None}];master,flags=dryrun.grounded_prices(raw,'$10/unit, regular $13/unit for 20 units',desired)
check('fixed_units_preserve_unit_and_total_basis',master['discount_price']==200 and master['regular_price']==260 and desired[0]['unit_price']==10,{'master_price':master['discount_price'],'item':desired[0]})
desired=[{'delivered_units':True,'unit_price':None}];master,flags=dryrun.grounded_prices(raw,'$10/unit, regular $13/unit for 20 units',desired)
check('invalid_boolean_quantity_rejected',desired[0]['delivered_units'] is None and 'invalid_fixed_quantity' in flags,{'item':desired[0],'flags':flags})
desired=[{'delivered_units':30,'unit_price':None},{'delivered_units':1,'unit_price':None}];master,flags=dryrun.grounded_prices({'discount_price':599,'is_package':True,'price_model':'total','items':[{},{}]},'Botox 30 units plus facial package total $599',desired)
check('package_price_not_distributed',master['discount_price']==599 and all(x['unit_price'] is None for x in desired),desired)
check('savings_is_not_sale_price',not dryrun.literal_price_supported('discount_price',50,'$50 off your first Hydrafacial') and dryrun.literal_price_supported('discount_amount',50,'$50 off your first Hydrafacial'),{'quote':'$50 off your first Hydrafacial'})
check('strikethrough_price_roles',dryrun.literal_price_supported('regular_price',1250,'Face: ~$1250,~ $875') and dryrun.literal_price_supported('discount_price',875,'Face: ~$1250,~ $875') and not dryrun.literal_price_supported('regular_price',875,'Face: ~$1250,~ $875'),{'regular':1250,'sale':875})
master,flags=dryrun.grounded_prices({'regular_price':400,'discount_price':99,'price_model':'total','items':[{}]},'Facial special $99',[{'delivered_units':None,'unit_price':None}])
check('original_price_not_reverse_inferred',master['regular_price'] is None and master['discount_percent'] is None,master)
check('review_dates_are_not_campaign_dates',not dryrun.explicit_date('2026-09-30','Jessica DePetro customer review 9/30/2026') and bool(dryrun.explicit_date('2026-09-30','Offer expires on September 30, 2026.')),{'rejected':'customer review 9/30/2026','accepted':'Offer expires on September 30, 2026.'})
check('historical_campaign_prices_require_review',all(r['action']=='待核验' for r in rows if r['offer_id'] in [8664,8674,8661,8562] and r['field']=='discount_price'),{'offer_ids':[8664,8674,8661,8562]})
check('discount_rounding_conflict_not_direct_update',all(r['action']=='待核验' for r in rows if r['offer_id']==9223 and r['field']=='discount_percent'),{'offer_id':9223,'database_percent':'60.40','website_percent':'60','regular_price':'1250','sale_price':'495'})
check('membership_fees_excluded',not any(r['field']=='candidate_offer' and __import__('re').search(r'(?:/\s*month|per month|monthly|sign.?up fee)',r['evidence_quote'],__import__('re').I) and __import__('re').search(r'member|sign.?up',r['evidence_quote'],__import__('re').I) for r in rows),{'excluded_extraction_candidates':sum(p.get('excluded_membership_fee_count',0) for p in pages)})
sql_commands=[re.sub(r"'(?:''|[^'])*'", "''",q) for q in final['queries']]
check('database_validation_queries_read_only',all(re.search(r'\b(?:insert|update|delete|create|alter|drop|truncate|call|do|copy|grant|revoke)\b',q,re.I) is None for q in sql_commands),{'query_count':len(sql_commands)})
tree=ast.parse((ROOT/'one-off/20261010_monitor_dryrun.py').read_text())
forbidden={'run_monitor','prepare_event','apply_plan','recover_journal','target_client','database_snapshot','deploy','atomic_json','render_page','extract_evidence'}
calls={n.func.id if isinstance(n.func,ast.Name) else n.func.attr if isinstance(n.func,ast.Attribute) else '' for n in ast.walk(tree) if isinstance(n,ast.Call)}
check('read_only_entry_points',not (calls & forbidden),{'forbidden_calls_found':sorted(calls & forbidden),'database_queries':'MCP SELECT/WITH SELECT only','scrape_and_OCR_calls':0,'task_or_monitor_calls':0})
server=read(OUT/'server_read_validation.json')
check('server_baselines_and_report_unchanged',server['baseline_comparison']['unchanged'],{'read_at':server['read_at'],'different_files':server['baseline_comparison']['different_files']})
old_mismatches=[]
for r in rows:
    record=promos.get(r['promotion_id']) if r['table']=='clinic_promotions' else items.get(r['item_id']) if r['table']=='promo_offer_items' else offers.get(r['offer_id']) if r['table']=='promo_offer_master' else None
    if record is not None and r['field'] in record and r['old_value']!=record[r['field']]:old_mismatches.append(r['row_id'])
check('reported_old_values_match_database_snapshot',not old_mismatches,old_mismatches)
bad_supplements=[]
for r in rows:
    if r['action'] in ['需更新','可新增'] and r['evidence_kind'] in ['existing_today_ocr','existing_today_browser']:
        relative=str(Path(r['evidence_path']).relative_to(OUT/'server_evidence'))
        modified=server['supplement_file_modified_at'].get(relative,'')
        if modified[:10]!='2026-10-10':bad_supplements.append(r['row_id'])
check('saved_browser_and_ocr_dates_corroborated',not bad_supplements,{'server_timezone':server['timezone_names'],'unverified_dates':bad_supplements,'timestamps':'file modification and plan processing times, not exact capture times'})
verdict='PASS' if all(x['result']=='PASS' for x in results) else 'FIX'
output={'verdict':verdict,'verified_at':datetime.now(timezone.utc).isoformat(),'route':'codex-prove Solo, single owner','meaning':'Read-only dry-run coverage/provenance passed; unresolved rows are not approved database updates.','checks':results}
(OUT/'verification.json').write_text(json.dumps(output,ensure_ascii=False,indent=2))
if verdict=='PASS':
    from zoneinfo import ZoneInfo
    def localtime(value):return datetime.fromisoformat(value).astimezone(ZoneInfo('Asia/Shanghai')).strftime('%Y-%m-%d %H:%M:%S')
    def cell(value):return str(value).replace('|','\\|').replace('\n',' ')
    summary=read(OUT/'summary.json');actions=Counter(r['action'] for r in rows)
    confirmed=[r for r in rows if r['action']=='需更新'];content=[r for r in confirmed if r['field']=='promotion_content'];price=[r for r in confirmed if r['table']=='promo_offer_master']
    report=['# 实时内容与数据库 dry-run 对比（2026-10-10）','',
        '**PROVE：PASS（Solo，单负责人）。** 189 个配置页面全部纳入，证据和数据库关联可追溯。PASS 表示这份只读比较报告通过验收；不表示待核验问题已解决，也不授权执行表中的建议。','',
        f'比较范围：Supabase `kdlpkjzcnbkjcvwsvlwn` 的 {len(promos)} 个活动、{len(offers)} 个 Offer、{len(items)} 个 Item；Offer 中 {sum(o["is_active"] for o in offers.values())} 个 active、{sum(not o["is_active"] for o in offers.values())} 个 inactive。另读同商家其他来源的 21 个活动、46 个 Offer、50 个 Item，防止误列新增；606 条服务元数据只用于关联核对。','',
        f'数据库读取时间（北京时间）：{localtime(db["read_started_at"])} 至 {localtime(db["read_finished_at"])}；末次 ID/版本核验时间：{localtime(current["read_at"])}。分页读取并非单一数据库事务；末次读取确认本范围 ID 集合和 `updated_at` 未变。','',
        '## 结果数量','',
        '| 建议动作 | CSV 行数 | 涉及配置页面数 | 含义 |',
        '|---|---:|---:|---|']
    meanings={'需更新':f'{len(price)} 项价格字段修正，涉及 3 个 Offer；{len(content)} 项活动正文片段补齐建议。','建议停用':'没有现有证据足以直接确认停用的记录。','可新增':'有可定位的当前优惠原文，尚缺对应活动；仍须解决来源归属、服务或条件问题。','未发现差异':'仅表中列明的已比较字段一致，不表示整页、全部优惠或所有字段一致。','待核验':'来源、活动身份、日期、图片、价格口径、条件或匹配仍有缺口。'}
    for action in ['需更新','建议停用','可新增','未发现差异','待核验']:
        report.append(f'| {action} | {actions[action]} | {summary["page_actions"][action]} | {meanings[action]} |')
    report.extend(['',f'共 {len(rows)} 行，每行是一项字段差异、一个候选优惠或一个待核验问题；行数不是唯一优惠数。页面分类可重叠，不能相加。189 个配置 URL 规范化后为 179 个来源。123 个 Offer 至少完成部分字段比较；其余 active 记录未能唯一对应现有证据，inactive 历史保留。','',
        '所有“需更新”和“可新增”行都注明执行前阻塞事项。没有直接可执行的写入批次；本次仅生成差异表，未分配新增数据库 ID。','',
        '## 已证实的价格字段问题','',
        'Allure 的激光脱毛套餐优惠价与原文一致，但三条 Offer 的原价均误存为 $2,500，使折扣金额和百分比也错误。建议保留 Offer/Item ID 和套餐总价口径，不分摊 Item 单价。折扣金额与百分比由原文直接给出的原价及优惠价计算，未反推原价。','',
        '| Promotion / Offer | 对应部位 | 原价：数据库 → 原文 | 优惠价 | 折扣金额：数据库 → 建议 | 折扣：数据库 → 建议 |',
        '|---|---|---:|---:|---:|---:|',
        '| 217 / 8129 | Face（Lip + Chin + Sideburns） | $2,500 → $1,250 | $875 | $1,625 → $375 | 65% → 30% |',
        '| 217 / 8130 | Face + Beard | $2,500 → $1,500 | $1,050 | $1,450 → $450 | 58% → 30% |',
        '| 217 / 8136 | Legs（Knees and Below） | $2,500 → $2,000 | $1,400 | $1,100 → $600 | 44% → 30% |','',
        '直接原文分别为 `Face (Lip + Chin + Sideburns): ~$1250,~ $875`、`Face + Beard: ~$1500,~ $1050`、`Legs (Knees and Below): ~$2000,~ $1400`。来源为 [Allure monthly specials](https://www.alluremedspa.com/monthly-specials)，原始正文文件和逐行采集时间见 CSV。现有完整写入校验仍有服务解析/数量上下文缺口，表中保留这些阻塞事项；明确价格差异没有被隐藏。','',
        '## 活动正文需补齐的事实','',
        '以下为已定位原文且旧正文未表达的金额/折扣事实，只建议补齐核验片段。建议值含 `replace_whole_content=false`，不能据此整段替换活动正文或删除旧条款。','',
        '| 商家 | Promotion ID | 当前原文片段 |',
        '|---|---:|---|'])
    for r in content:report.append(f'| {cell(r["business_name"])} | {r["promotion_id"]} | {cell(r["evidence_quote"][:220])} |')
    report.extend(['','## 重要的待核验问题','',
        '- New Look：女性全腿旧价 $135、当前原文 $134；女性 Full Bikini + Underarms 旧价 $58、当前 $57。来源关联两条活动（2、3587），且存储的旧活动日期已过，先确认活动归属。男性胸腹、全背附赠颈部的条款不能仅按部位名称当作同一优惠改价；多次套餐与单次治疗也不能直接覆盖。',
        '- Avere：同 URL 活动 902、3598 均保留；不能凭 URL 选一条覆盖。FacesPlus 同时存在 10% Tox 条款的不同计价口径记录（10434、10720），保留匹配歧义。',
        '- BioRevive Offer 9223：网站宣传 60% OFF，但 $1,250 → $495 的计算折扣为 60.4%，恰与数据库一致。列为取整/表达口径核验，不能改折扣后让触发器把售价变成 $500。',
        '- 14 个 active 活动的数据库结束日期为 2026-09-30。旧日期只是线索；页面可能已展示十月新活动，不能仅凭数据库旧日期直接停用。Beauty & Health by Liz 页面明确出现 2023 年过期文案，但对应活动 162 已 inactive，不重新激活；Green Spring 的 2018 历史活动 626 同样保留 inactive。',
        '- Beauty Bungalow 的 $89/$199 Intro 优惠在同商家另一来源已存在（9813、9814）；不能误列两条新增。Beauty With Bubbly 的两个 URL 展示同一四次 DiamondGlow/Microneedling 混选套餐，列为来源合并核验。',
        '- Preventative Medical Clinic 的 $99/$249/$349 是每月付款，有一年承诺期，不能作为完整套餐总价。Belle Âme 的 November 标题、Mother’s Day 礼卡和未证明年份的季节活动不直接建议激活。',
        '- 套餐切换、最低购买数量、固定交付数量、封顶前若干单位、附赠内容和资格条件均保留在差异或审查行中；未提供的价格字段不清空。','',
        '## 新增候选与保护','',
        '14 条新增候选来自 6 个页面：Skin Perfect 五个分店页面共 13 条（$599 微针+颈部/增强剂、$699 VI Peel+两部位 Toxin、20% 激光/换肤折扣、Glendora $849 Asclera 等），以及 Preventative 的 $49 已治疗部位 touch-up。均未分配 ID，先确认分店/来源归属、服务与资格条件。包月套餐、跨来源已有优惠和季节时效不明的候选列在待核验区。','',
        f'人工保护表共 8 个审查来源条目，对应 4 个不同 Offer ID（7594、8590、10030、10517）；本比较范围包含 8590、10030、10517。CSV 的 {len(protected_rows)} 行标明保护，父活动建议也标明其受保护后代。保留历史审查记录，不因一次新抓取覆盖人工决定。','',
        '本范围 active blanket Offer 的只读关联检查返回 0 行异常。范围性优惠保持 `service_id=NULL`，不强行绑定某个具体服务。','',
        '## 证据范围与只读验证','',
        '最新 WSL 任务的原始响应和 Markdown 是主要证据：153 页有可用正文，36 页失败、空正文或错误状态，其中 6 页没有已保存基线。失败时保留旧快照路径作为历史引用，明确标记本次失败；不能以旧快照声称当前报价，也不能据此下架。',
        '已有 Thinkbook Markdown、已保存浏览器正文和可读 OCR 仅作补充。未重抓页面、未补截图/OCR、未调用模型提取。证据未能定位的图片报价仍待核验，完整页面未获得或条款匹配不清楚时不推断移除。',
        '逐行 `captured_at` 优先使用原始抓取时间；Thinkbook 文件仅有批次完成时间，浏览器/OCR 仅有处理时间和文件修改时间，均明确注明时间精度。WSL 使用 PST/PDT，因此提取的 `observed_date=2026-10-09` 与北京时间 10 月 10 日下午的处理时间相对应。',
        f'验证共 {len(results)} 项通过：189 页覆盖、完整 ID/关联、旧值和末次版本、原文定位、价格角色、数量与套餐、人工保护、历史状态、失效日期、失败/图片证据和 CSV 一致性。服务器四组 baseline、运行报告和保护文件末次回读与导出时完全相同。',
        'Supabase 仅调用 MCP SELECT；没有数据库写入、DDL、迁移、任务入口调用、定时任务修改或基线推进。报告生成程序只读已保存输入并写入本地输出。','',
        '## 文件与字段说明','',
        f'- [dryrun_changes.csv]({OUT}/dryrun_changes.csv)：完整差异/待核验表（UTF-8 BOM）。',
        f'- [dryrun_evidence.json]({OUT}/dryrun_evidence.json)：数据库快照、所有关联 ID 清单、189 页覆盖、证据路径、保护和逐行建议。',
        f'- [verification.json]({OUT}/verification.json)：逐项验证结果。','',
        '`promotion_id / offer_id / item_id` 为空表示没有唯一现有目标或仅为候选；`old_value / proposed_value` 区分 `null`、空数组和布尔值；`execution_blockers` 标明后续执行所需解决的问题。`evidence_kind / evidence_path / support_path / captured_at / database_read_at` 用于追溯证据来源、时间和数据库版本。','',
        '## 189 页覆盖索引','',
        '每个配置页面都有结果；URL 别名规范化后可以共享差异行。完整证据引用及 Offer/Item 清单见 JSON。','',
        '| 序号 | URL | 主证据 | Promotion ID（全部） | Offer / Item 数 | 结果类别 |',
        '|---:|---|---|---|---:|---|'])
    for index,p in enumerate(pages,1):
        report.append(f'| {index} | {cell(p["url"])} | {"可用" if p["primary_status"]=="available" else "失败/为空"} | {", ".join(map(str,p["promotion_ids"])) or "无"} | {len(p["offer_ids"])} / {len(p["item_ids"])} | {"、".join(p["actions"])} |')
    (OUT/'dryrun_report.md').write_text('\n'.join(report)+'\n')
print(json.dumps({'verdict':verdict,'checks':len(results),'failures':[x for x in results if x['result']!='PASS']},ensure_ascii=False,indent=2))
raise SystemExit(0 if verdict=='PASS' else 1)
