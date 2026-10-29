-- CostFinder clinic_services.regular_price: read-only dry-run preview
-- Generated 2026-10-01 from the frozen 217-row snapshot and 51-row QA mapping.
-- SELECT only: no UPDATE, INSERT, DELETE, DDL, or database writes.
-- Project: costfinder (kdlpkjzcnbkjcvwsvlwn).
-- Run this file as one script in a read-only transaction.
-- Output: service candidates plus all linked Offer items; no writes are made.
-- Live-trigger and proposed-migration projections intentionally use their
-- respective current definitions. A difference is surfaced instead of resolved
-- because per_unit + delivered_units semantics need an operator decision.
-- Proposed-migration projection uses equality-based provenance; an explicit
-- source price equal to the old derived value cannot be distinguished as such.

BEGIN TRANSACTION READ ONLY;

WITH candidates (service_id,business_id,qa_verdict,proposed_action,old_regular_price,proposed_regular_price,old_service_name,old_unit_type,old_service_area,proposed_service_area,old_source_url,proposed_source_url,baseline_updated_at,rationale) AS (
VALUES
  (14::bigint, 111::bigint, 'FAIL_MISMATCH'::text, 'CLEAR_PRICE'::text, 12::numeric, NULL::numeric, 'Botox'::text, 'unit'::text, 'face'::text, 'face'::text, 'https://www.bespokebeautyusa.com/services/botox'::text, 'https://www.bespokebeautyusa.com/services/botox'::text, '2026-09-07T06:32:23.462205+00:00'::timestamptz, '当前来源已不支持 $12/unit，替代的当前正常价尚无充分页面证据。'::text),
  (35::bigint, 246::bigint, 'FAIL_MISMATCH'::text, 'CLEAR_PRICE'::text, 499::numeric, NULL::numeric, 'Dermal Filler'::text, 'syringe'::text, NULL::text, NULL::text, 'https://www.nakedmd.com/products/lip-filler'::text, 'https://www.nakedmd.com/products/lip-filler'::text, '2026-09-18T23:27:19.536484+00:00'::timestamptz, '来源是具体唇部/variant filler 定价，不能作为 generic filler 的统一 syringe 正常价。'::text),
  (236::bigint, 1019::bigint, 'FAIL_MISMATCH'::text, 'CLEAR_PRICE'::text, 15::numeric, NULL::numeric, 'Botox'::text, 'unit'::text, 'face'::text, 'face'::text, 'https://longevityok.com/products/botox'::text, 'https://longevityok.com/products/botox'::text, '2026-09-08T08:43:06.141547+00:00'::timestamptz, '当前产品价格结构明显低于 $15/unit；具体可复核的非会员正常价未确认。'::text),
  (260::bigint, 1194::bigint, 'FAIL_MISMATCH'::text, 'CLEAR_PRICE'::text, 10::numeric, NULL::numeric, 'Botox'::text, 'unit'::text, NULL::text, NULL::text, 'https://vasuskinsolutions.com/botox-services-boulder-co'::text, 'https://vasuskinsolutions.com/botox-services-boulder-co'::text, '2026-09-15T06:29:27.674455+00:00'::timestamptz, '现有 $10 不受当前来源支持；$11 为会员价，虽然 QA 提到 $14 普通价，当前源页尚未提供可复核的 $14 正常价。'::text),
  (269::bigint, 1219::bigint, 'FAIL_UNSUPPORTED'::text, 'CLEAR_PRICE'::text, 879::numeric, NULL::numeric, 'Dermal Filler'::text, 'syringe'::text, NULL::text, NULL::text, 'https://www.luxeroomcosmetic.com/facial-balancing-the-subtle-art-behind-great-filler'::text, 'https://www.luxeroomcosmetic.com/facial-balancing-the-subtle-art-behind-great-filler'::text, '2026-09-18T23:27:19.536484+00:00'::timestamptz, '文章没有 $879 normal-price 证据。'::text),
  (292::bigint, 1372::bigint, 'FAIL_UNSUPPORTED'::text, 'CLEAR_PRICE'::text, 12::numeric, NULL::numeric, 'Botox'::text, 'unit'::text, NULL::text, NULL::text, 'https://belleamemedspa.com/botox-dysport-daxxify-xeomin-belle-ame-med-spa'::text, 'https://belleamemedspa.com/botox-dysport-daxxify-xeomin-belle-ame-med-spa'::text, '2026-09-18T23:27:19.536484+00:00'::timestamptz, '当前 service page 没有 $12 normal-price 证据；其他会员内容不是正常价证明。'::text),
  (297::bigint, 1379::bigint, 'FAIL_MISMATCH'::text, 'CLEAR_PRICE'::text, 800::numeric, NULL::numeric, 'Dermal Filler'::text, 'syringe'::text, NULL::text, NULL::text, 'https://www.zenaestheticsandwellness.com/dermal-filler'::text, 'https://www.zenaestheticsandwellness.com/dermal-filler'::text, '2026-09-08T08:55:41.058671+00:00'::timestamptz, '来源给出 filler $600–$800/syringe 范围，不支持 generic filler 固定 $800。'::text),
  (322::bigint, 1469::bigint, 'FAIL_UNSUPPORTED'::text, 'CLEAR_PRICE'::text, 13::numeric, NULL::numeric, 'Botox'::text, 'unit'::text, 'face'::text, 'face'::text, 'https://www.halcyonmindbody.com/memberships/'::text, 'https://www.halcyonmindbody.com/memberships/'::text, '2026-09-07T06:32:23.462205+00:00'::timestamptz, '页面只证明 $12/unit membership benefit，不支持 $13 normal price。'::text),
  (359::bigint, 1702::bigint, 'FAIL_UNSUPPORTED'::text, 'CLEAR_PRICE'::text, 550::numeric, NULL::numeric, 'Dermal Filler'::text, 'syringe'::text, NULL::text, NULL::text, 'https://www.trubalancedaesthetics.com/dermal-fillers-in-superior-co'::text, 'https://www.trubalancedaesthetics.com/dermal-fillers-in-superior-co'::text, '2026-09-18T23:27:19.536484+00:00'::timestamptz, 'exact service page 没有 normal-price 证据。'::text),
  (686::bigint, 2173::bigint, 'FAIL_UNSUPPORTED'::text, 'CLEAR_PRICE'::text, 8::numeric, NULL::numeric, 'Botox'::text, 'unit'::text, 'face'::text, 'face'::text, 'http://alliancehealthchoice.com'::text, 'http://alliancehealthchoice.com'::text, '2026-09-15T06:29:27.674455+00:00'::timestamptz, '首页只写 starting $8，未说明 $8/unit。'::text),
  (687::bigint, 2173::bigint, 'FAIL_MISMATCH'::text, 'CLEAR_PRICE'::text, 450::numeric, NULL::numeric, 'Dermal Filler'::text, 'syringe'::text, NULL::text, NULL::text, 'https://alliancehealthchoice.com/'::text, 'https://alliancehealthchoice.com/'::text, '2026-09-18T23:27:19.536484+00:00'::timestamptz, '$450 对应 Under Eye Filler，当前行却是无区域/variant 的 generic Dermal Filler。'::text),
  (758::bigint, 2227::bigint, 'FAIL_MISMATCH'::text, 'UPDATE_PRICE'::text, 18::numeric, 13::numeric, 'Botox'::text, 'unit'::text, 'face'::text, 'face'::text, 'https://cienegaspa.com/services/botox-pain-relief'::text, 'https://cienegaspa.com/services/dysport'::text, '2026-09-07T06:32:23.462205+00:00'::timestamptz, 'Cienega 当前价格页列 Botox $13/unit；旧 $18 不符。两个 per-unit linked offers 的 discount_price 均为 $14，高于候选正价 $13，migration 会保留旧 Offer 正价；先解决冲突。'::text),
  (803::bigint, 2251::bigint, 'FAIL_MISMATCH'::text, 'CLEAR_PRICE'::text, 800::numeric, NULL::numeric, 'Dermal Filler'::text, 'syringe'::text, NULL::text, NULL::text, 'https://totalwomenhealthcare.com/dermal-fillers-santa-ana'::text, 'https://totalwomenhealthcare.com/dermal-fillers-santa-ana'::text, '2026-09-18T23:27:19.536484+00:00'::timestamptz, '$800 对应特定产品/variant；同源 generic filler 有多个档位。'::text),
  (1320::bigint, 2522::bigint, 'FAIL_MISMATCH'::text, 'CLEAR_PRICE'::text, 11::numeric, NULL::numeric, 'Botox'::text, 'unit'::text, NULL::text, NULL::text, 'https://goldcoastaesthetics.com/botox-xeomin-and-daxxify'::text, 'https://goldcoastaesthetics.com/botox-xeomin-and-daxxify'::text, '2026-09-07T06:32:23.462205+00:00'::timestamptz, 'Gold Coast 当前页面未支持 $11/unit；$15 证据来自 2024 PDF，不能单独证明当前正常价。'::text),
  (1780::bigint, 2756::bigint, 'FAIL_MISMATCH'::text, 'CLEAR_PRICE'::text, 7::numeric, NULL::numeric, 'Botox'::text, 'unit'::text, NULL::text, NULL::text, 'https://execesthetics.com/services'::text, 'https://execesthetics.com/services'::text, '2026-09-09T00:32:13.54478+00:00'::timestamptz, '来源只支持通用 Neurotoxin $7，不能收窄成 Botox $7/unit。'::text),
  (1808::bigint, 2770::bigint, 'FAIL_UNSUPPORTED'::text, 'CLEAR_PRICE'::text, 14::numeric, NULL::numeric, 'Botox'::text, 'unit'::text, NULL::text, NULL::text, 'https://www.skincredibledermatology.com/cosmetic-dermatology/botox'::text, 'https://www.skincredibledermatology.com/cosmetic-dermatology/botox'::text, '2026-09-18T23:27:19.536484+00:00'::timestamptz, '来源介绍 Botox，但没有公开价格。'::text),
  (1898::bigint, 2932::bigint, 'FAIL_MISMATCH'::text, 'CLEAR_PRICE'::text, 9::numeric, NULL::numeric, 'Botox'::text, 'unit'::text, NULL::text, NULL::text, 'https://smoothmd.com/products/botox'::text, 'https://smoothmd.com/products/botox'::text, '2026-09-18T23:27:19.536484+00:00'::timestamptz, '当前页面的 $12/$9 处于产品销售价/特殊价语境，不能证明旧 $9 是当前正常 Botox 单位价。'::text),
  (1939::bigint, 3049::bigint, 'FAIL_MISMATCH'::text, 'CLEAR_PRICE'::text, 14::numeric, NULL::numeric, 'Dysport'::text, 'unit'::text, NULL::text, NULL::text, 'https://fixxmedicalspa.com/treatments/injectables'::text, 'https://fixxmedicalspa.com/treatments/injectables'::text, '2026-09-07T06:32:25.93205+00:00'::timestamptz, '来源写 $14 per 3 Dysport units；禁止在本 dry-run 擅自换算成 $4.67/unit。'::text),
  (1950::bigint, 3095::bigint, 'FAIL_UNSUPPORTED'::text, 'CLEAR_PRICE'::text, 12.5::numeric, NULL::numeric, 'Botox'::text, 'unit'::text, NULL::text, NULL::text, 'https://www.beautyandbodylounge.com/monthly-specials/'::text, 'https://www.beautyandbodylounge.com/monthly-specials/'::text, '2026-09-15T05:56:49.666944+00:00'::timestamptz, 'monthly-specials 页面不能支撑 $12.50 normal Botox 单位价。'::text),
  (1951::bigint, 3095::bigint, 'FAIL_UNSUPPORTED'::text, 'CLEAR_PRICE'::text, 4.75::numeric, NULL::numeric, 'Dysport'::text, 'unit'::text, NULL::text, NULL::text, 'https://www.beautyandbodylounge.com/monthly-specials/'::text, 'https://www.beautyandbodylounge.com/monthly-specials/'::text, '2026-09-15T05:56:49.666944+00:00'::timestamptz, 'monthly-specials 页面不能支撑 $4.75 normal Dysport 单位价。'::text),
  (1952::bigint, 3095::bigint, 'FAIL_UNSUPPORTED'::text, 'CLEAR_PRICE'::text, 11::numeric, NULL::numeric, 'Xeomin'::text, 'unit'::text, NULL::text, NULL::text, 'https://www.beautyandbodylounge.com/monthly-specials/'::text, 'https://www.beautyandbodylounge.com/monthly-specials/'::text, '2026-09-15T05:56:49.666944+00:00'::timestamptz, 'monthly-specials 页面不能支撑 $11 normal Xeomin 单位价。'::text),
  (1953::bigint, 3095::bigint, 'FAIL_MISMATCH'::text, 'CLEAR_PRICE'::text, 2000::numeric, NULL::numeric, 'Dermal Filler'::text, 'syringe'::text, NULL::text, NULL::text, 'https://www.beautyandbodylounge.com/monthly-specials/'::text, 'https://www.beautyandbodylounge.com/monthly-specials/'::text, '2026-09-18T23:27:19.536484+00:00'::timestamptz, '$2,000 属于多 syringe/package 结构，不能当作单 syringe 正常价。'::text),
  (2253::bigint, 3071::bigint, 'FAIL_MISMATCH'::text, 'CLEAR_PRICE'::text, 14::numeric, NULL::numeric, 'Dysport'::text, 'unit'::text, NULL::text, NULL::text, 'https://cosmetiqmedicine.com/treatments/botox-dysport-vancouver-wa'::text, 'https://cosmetiqmedicine.com/treatments/botox-dysport-vancouver-wa'::text, '2026-09-18T23:27:19.536484+00:00'::timestamptz, '当前来源的 Dysport 单位价不是 $14；本批没有可靠的替代正常价证据。'::text),
  (2306::bigint, 2932::bigint, 'FAIL_MISMATCH'::text, 'CLEAR_PRICE'::text, 8::numeric, NULL::numeric, 'Xeomin'::text, 'unit'::text, NULL::text, NULL::text, 'https://smoothmd.com/collections/all-treatments'::text, 'https://smoothmd.com/collections/all-treatments'::text, '2026-09-18T23:27:19.536484+00:00'::timestamptz, 'SmoothMD 当前页面含 $8/$12 等销售价结构，不能证明旧 $8 是当前正常 Xeomin 单位价。'::text),
  (2328::bigint, 3097::bigint, 'FAIL_UNSUPPORTED'::text, 'CLEAR_PRICE'::text, 750::numeric, NULL::numeric, 'Dermal Filler'::text, 'syringe'::text, NULL::text, NULL::text, 'https://allurantmedical.com/chin-filler-san-diego'::text, 'https://allurantmedical.com/chin-filler-san-diego'::text, '2026-09-18T23:27:19.536484+00:00'::timestamptz, 'chin-filler 页面没有足够证据支持 generic $750/syringe。'::text),
  (2333::bigint, 1481::bigint, 'FAIL_MISMATCH'::text, 'CLEAR_PRICE'::text, 850::numeric, NULL::numeric, 'Juvéderm'::text, 'syringe'::text, NULL::text, NULL::text, 'https://www.revivemedspaokc.com/pricing'::text, 'https://www.revivemedspaokc.com/pricing'::text, '2026-09-18T23:27:19.536484+00:00'::timestamptz, '$850 仅适用于 Volux；其他 Juvéderm variants 为不同价位。'::text),
  (2345::bigint, 2173::bigint, 'FAIL_MISMATCH'::text, 'CLEAR_PRICE'::text, 239::numeric, NULL::numeric, 'Juvéderm'::text, 'syringe'::text, NULL::text, NULL::text, 'https://alliancehealthchoice.com/'::text, 'https://alliancehealthchoice.com/'::text, '2026-09-18T23:27:19.536484+00:00'::timestamptz, '$239 是 half filler，不能作为完整 syringe 单位价，也不做 ×2 推算。'::text),
  (2347::bigint, 1019::bigint, 'FAIL_UNSUPPORTED'::text, 'CLEAR_PRICE'::text, 11.5::numeric, NULL::numeric, 'Dysport'::text, 'unit'::text, NULL::text, NULL::text, 'https://longevityok.com/pages/membership'::text, 'https://longevityok.com/pages/membership'::text, '2026-09-18T23:27:19.536484+00:00'::timestamptz, '$11.50 明确是 membership benefit price，不是 regular/non-member 价格。'::text),
  (2350::bigint, 1219::bigint, 'FAIL_MISMATCH'::text, 'CLEAR_PRICE'::text, 13.98::numeric, NULL::numeric, 'Dysport'::text, 'unit'::text, NULL::text, NULL::text, 'https://www.luxeroomcosmetic.com/price-list'::text, 'https://www.luxeroomcosmetic.com/price-list'::text, '2026-09-09T00:32:13.54478+00:00'::timestamptz, '$13.98 是换算/等效剂量数值，不是 Dysport 的正常单位价。'::text),
  (2376::bigint, 2227::bigint, 'FAIL_MISMATCH'::text, 'UPDATE_PRICE'::text, 18::numeric, 5::numeric, 'Dysport'::text, 'unit'::text, NULL::text, NULL::text, 'https://cienegaspa.com/services/dysport'::text, 'https://cienegaspa.com/services/dysport'::text, '2026-09-18T23:27:19.536484+00:00'::timestamptz, 'Cienega Dysport 页面列 $5/unit；旧 $18 不符。per-unit linked offer 的 discount_price 为 $14，高于候选正价 $5，migration 会保留旧 Offer 正价；另一个无 delivered_units 的 from Offer 不具备确定派生输入。先解决冲突。'::text),
  (2498::bigint, 2173::bigint, 'FAIL_UNSUPPORTED'::text, 'CLEAR_PRICE'::text, 325::numeric, NULL::numeric, 'Radiesse'::text, 'syringe'::text, NULL::text, NULL::text, 'https://alliancehealthchoice.com'::text, 'https://alliancehealthchoice.com'::text, '2026-09-18T23:27:19.536484+00:00'::timestamptz, '来源只支持 starting $325，没有明确 /syringe。'::text),
  (2502::bigint, 1219::bigint, 'FAIL_UNSUPPORTED'::text, 'CLEAR_PRICE'::text, 879::numeric, NULL::numeric, 'Dermal Filler'::text, 'syringe'::text, NULL::text, NULL::text, 'https://www.luxeroomcosmetic.com/facial-balancing-the-subtle-art-behind-great-filler'::text, 'https://www.luxeroomcosmetic.com/facial-balancing-the-subtle-art-behind-great-filler'::text, '2026-09-09T00:32:13.54478+00:00'::timestamptz, '文章没有 $879 normal-price 证据。'::text),
  (2507::bigint, 1481::bigint, 'FAIL_MISMATCH'::text, 'CLEAR_PRICE'::text, 700::numeric, NULL::numeric, 'Restylane'::text, 'syringe'::text, NULL::text, NULL::text, 'https://www.revivemedspaokc.com/pricing'::text, 'https://www.revivemedspaokc.com/pricing'::text, '2026-09-18T23:27:19.536484+00:00'::timestamptz, 'Restylane variants 为不同价格，不支持 generic Restylane 固定 $700/syringe。'::text),
  (2514::bigint, 682::bigint, 'FAIL_MISMATCH'::text, 'CLEAR_PRICE'::text, 15::numeric, NULL::numeric, 'Dysport'::text, 'unit'::text, NULL::text, NULL::text, 'https://www.conciergeaesthetics.com/treatments/dermal-fillers/'::text, 'https://www.conciergeaesthetics.com/treatments/dermal-fillers/'::text, '2026-09-18T23:27:19.536484+00:00'::timestamptz, 'Dysport 行链接到 dermal-fillers 来源页，来源与 service 不匹配。'::text),
  (2530::bigint, 3070::bigint, 'FAIL_UNSUPPORTED'::text, 'CLEAR_PRICE'::text, 599::numeric, NULL::numeric, 'Dermal Filler'::text, 'syringe'::text, NULL::text, NULL::text, 'https://smileyaesthetics.com/events/special-training-sculptra-pdgf-nashville-06-20-2026'::text, 'https://smileyaesthetics.com/events/special-training-sculptra-pdgf-nashville-06-20-2026'::text, '2026-09-18T23:27:19.536484+00:00'::timestamptz, 'event/training special 不是 normal regular price。'::text),
  (2534::bigint, 2889::bigint, 'FAIL_MISMATCH'::text, 'CLEAR_PRICE'::text, 975::numeric, NULL::numeric, 'Radiesse'::text, 'syringe'::text, NULL::text, NULL::text, 'https://masters-medspa.com/pricing'::text, 'https://masters-medspa.com/pricing'::text, '2026-09-09T00:32:13.54478+00:00'::timestamptz, '$975 购买 1.8 syringes；按计划的保守口径不做 package 到单支的推算。'::text),
  (2544::bigint, 2857::bigint, 'FAIL_UNSUPPORTED'::text, 'CLEAR_PRICE'::text, 650::numeric, NULL::numeric, 'Dermal Filler'::text, 'syringe'::text, NULL::text, NULL::text, 'https://nubellamedspa.com/dermal-fillers'::text, 'https://nubellamedspa.com/dermal-fillers'::text, '2026-09-18T23:27:19.536484+00:00'::timestamptz, 'exact Nubella filler 页面没有公开 normal price。'::text),
  (2638::bigint, 2756::bigint, 'FAIL_UNSUPPORTED'::text, 'CLEAR_PRICE'::text, 350::numeric, NULL::numeric, 'Dermal Filler'::text, NULL::text, NULL::text, NULL::text, 'https://execesthetics.com/services'::text, 'https://execesthetics.com/services'::text, '2026-09-22T02:50:17.19567+00:00'::timestamptz, 'Executive Esthetics 服务页列 filler 服务但没有价格；unit_type 也未明确。'::text),
  (2639::bigint, 2889::bigint, 'FAIL_MISMATCH'::text, 'CLEAR_PRICE'::text, 975::numeric, NULL::numeric, 'Dermal Filler'::text, 'syringe'::text, NULL::text, NULL::text, 'https://masters-medspa.com/our-services/dermal-fillers'::text, 'https://masters-medspa.com/our-services/dermal-fillers'::text, '2026-09-09T00:32:13.54478+00:00'::timestamptz, 'Generic filler 行吸收了具体 package/biostimulator 定价，不是单支 generic filler 正常价。'::text),
  (2650::bigint, 2167::bigint, 'FAIL_MISMATCH'::text, 'CLEAR_PRICE'::text, 1500::numeric, NULL::numeric, 'Radiesse'::text, NULL::text, NULL::text, NULL::text, 'https://rawbeautyaesthetics.com/prices'::text, 'https://rawbeautyaesthetics.com/prices'::text, '2026-09-22T02:50:17.19567+00:00'::timestamptz, '$1,500 对应 Radiesse Neck Wash；普通 Radiesse 标价 $850，但 unit_type 为空，需先确认服务/单位语义。'::text),
  (2656::bigint, 3020::bigint, 'FAIL_UNSUPPORTED'::text, 'CLEAR_PRICE'::text, 259::numeric, NULL::numeric, 'Hydrafacial'::text, 'session'::text, NULL::text, NULL::text, 'https://californiacosmetics.com/products/hydrafacial-bundle'::text, 'https://californiacosmetics.com/products/hydrafacial-bundle'::text, '2026-09-15T06:29:27.674455+00:00'::timestamptz, 'Hydrafacial bundle 缺少明确的单次 normal price 结构。'::text),
  (2765::bigint, 682::bigint, 'FAIL_UNSUPPORTED'::text, 'CLEAR_PRICE'::text, 900::numeric, NULL::numeric, 'Dermal Filler'::text, 'syringe'::text, NULL::text, NULL::text, 'https://www.conciergeaesthetics.com/treatments/dermal-fillers/'::text, 'https://www.conciergeaesthetics.com/treatments/dermal-fillers/'::text, '2026-09-18T23:27:19.536484+00:00'::timestamptz, 'Radiesse treatments $900+ 不能证明 generic filler $900/syringe。'::text),
  (2801::bigint, 2978::bigint, 'FAIL_MISMATCH'::text, 'CLEAR_PRICE'::text, 1250::numeric, NULL::numeric, 'Morpheus8'::text, NULL::text, 'full face'::text, 'full face'::text, 'https://biorevivespa.com/products/morpheus8-body'::text, 'https://biorevivespa.com/products/morpheus8-body'::text, '2026-09-22T02:50:17.19567+00:00'::timestamptz, '当前 Morpheus8 页面定价结构已变，未确认可替代的当前正常价。'::text),
  (2841::bigint, 2090::bigint, 'FAIL_MISMATCH'::text, 'CLEAR_PRICE'::text, 600::numeric, NULL::numeric, 'Laser Hair Removal'::text, 'session'::text, NULL::text, NULL::text, 'https://beautybungalowsculpting.com/promos-page'::text, 'https://beautybungalowsculpting.com/promos-page'::text, '2026-09-18T23:27:19.536484+00:00'::timestamptz, '当前来源是 body-sculpting promo，不能支撑 Laser Hair Removal $600/session。'::text),
  (2846::bigint, 2645::bigint, 'FAIL_MISMATCH'::text, 'FIX_AREA_MAPPING'::text, 395::numeric, 395::numeric, 'Laser Hair Removal'::text, 'session'::text, NULL::text, 'Bikini'::text, 'https://drlaserla.com/laser-hair-removal-botox-juvederm-coolsculpting-non-surgical-face-and-skin-care-treatments'::text, 'https://drlaserla.com/sale/'::text, '2026-09-18T23:27:19.536484+00:00'::timestamptz, '$395 明确是 Bikini session regular price；补 service_area=Bikini，保留价格。'::text),
  (2847::bigint, 2645::bigint, 'FAIL_MISMATCH'::text, 'FIX_AREA_MAPPING'::text, 220::numeric, 220::numeric, 'Laser Hair Removal'::text, 'session'::text, NULL::text, 'Underarm'::text, 'https://drlaserla.com/laser-hair-removal-botox-juvederm-coolsculpting-non-surgical-face-and-skin-care-treatments'::text, 'https://drlaserla.com/sale/'::text, '2026-09-18T23:27:19.536484+00:00'::timestamptz, '$220 明确是 Underarm session regular price；补 service_area=Underarm，保留价格。'::text),
  (2854::bigint, 2959::bigint, 'FAIL_UNSUPPORTED'::text, 'CLEAR_PRICE'::text, 268::numeric, NULL::numeric, 'Hydrafacial'::text, 'session'::text, NULL::text, NULL::text, 'https://www.ibeautyirvine.com/hydrafacial'::text, 'https://www.ibeautyirvine.com/hydrafacial'::text, '2026-09-18T23:27:19.536484+00:00'::timestamptz, 'exact source 当前没有价格证据。'::text),
  (2991::bigint, 3046::bigint, 'FAIL_MISMATCH'::text, 'CLEAR_PRICE'::text, 50::numeric, NULL::numeric, 'Facial'::text, 'session'::text, NULL::text, NULL::text, 'https://thefixclinic.com/facials'::text, 'https://thefixclinic.com/facials'::text, '2026-09-18T23:27:19.536484+00:00'::timestamptz, '$50 对应 add-on/dermaplaning，不是 generic Facial 正常价。'::text),
  (3024::bigint, 1372::bigint, 'FAIL_UNSUPPORTED'::text, 'CLEAR_PRICE'::text, 12::numeric, NULL::numeric, 'Neurotoxin'::text, 'unit'::text, NULL::text, NULL::text, 'https://belleamemedspa.com/belle-ame-bank'::text, 'https://belleamemedspa.com/belle-ame-bank'::text, '2026-09-18T23:27:19.536484+00:00'::timestamptz, 'Belle Ame Bank 页面不能证明 $12 是 normal/non-member regular price。'::text),
  (3032::bigint, 2070::bigint, 'FAIL_MISMATCH'::text, 'CLEAR_PRICE'::text, 600::numeric, NULL::numeric, 'Facial'::text, 'session'::text, NULL::text, NULL::text, 'https://www.sol-aesthetics.com/sol-self-care-club'::text, 'https://www.sol-aesthetics.com/sol-self-care-club'::text, '2026-09-18T23:27:19.536484+00:00'::timestamptz, '$600 对应 Microneedling with PRF，不是 generic Facial。'::text),
  (3295::bigint, 2173::bigint, 'FAIL_MISMATCH'::text, 'CLEAR_PRICE'::text, 359::numeric, NULL::numeric, 'Belotero'::text, 'syringe'::text, NULL::text, NULL::text, 'https://alliancehealthchoice.com/'::text, 'https://alliancehealthchoice.com/'::text, '2026-09-18T23:27:19.536484+00:00'::timestamptz, '来源支持 $359 treatment，但不支持 $359/syringe；在确认单位与正常价前清空旧的 syringe 解释。'::text)
),
candidate_offer_ids AS (
  SELECT DISTINCT i.offer_id
  FROM public.promo_offer_items i
  JOIN candidates c ON c.service_id = i.service_id
),
item_counts AS (
  SELECT i.offer_id, count(*)::bigint AS item_count
  FROM public.promo_offer_items i
  JOIN candidate_offer_ids co ON co.offer_id = i.offer_id
  GROUP BY i.offer_id
),
inputs AS (
  SELECT c.*,
         s.business_id AS live_business_id,
         s.service_name::text AS live_service_name,
         s.regular_price AS live_service_regular_price,
         s.unit_type::text AS live_unit_type,
         s.service_area AS live_service_area,
         s.source_url AS live_source_url,
         s.updated_at AS live_service_updated_at,
         i.offer_item_id, i.offer_id, i.item_name, i.delivered_units, i.unit_price,
         i.updated_at AS item_updated_at,
         m.offer_raw_text, m.regular_price AS current_offer_regular_price,
         m.discount_price, m.is_active, m.is_package, m.price_model,
         m.updated_at AS offer_updated_at, ic.item_count,
         (s.service_id IS NOT NULL
          AND s.business_id IS NOT DISTINCT FROM c.business_id
          AND s.service_name::text IS NOT DISTINCT FROM c.old_service_name
          AND s.regular_price IS NOT DISTINCT FROM c.old_regular_price
          AND s.unit_type::text IS NOT DISTINCT FROM c.old_unit_type
          AND s.service_area IS NOT DISTINCT FROM c.old_service_area
          AND s.source_url IS NOT DISTINCT FROM c.old_source_url) AS baseline_matches,
         (s.updated_at IS NOT DISTINCT FROM c.baseline_updated_at) AS baseline_updated_at_matches
  FROM candidates c
  LEFT JOIN public.clinic_services s ON s.service_id = c.service_id
  LEFT JOIN public.promo_offer_items i ON i.service_id = c.service_id
  LEFT JOIN public.promo_offer_master m ON m.id = i.offer_id
  LEFT JOIN item_counts ic ON ic.offer_id = i.offer_id
),
derivations AS (
  SELECT x.*,
    -- Exact OLD-value rule in the proposed migration function; gated by semantic baseline.
    CASE
      WHEN NOT x.baseline_matches OR x.offer_id IS NULL OR x.live_service_regular_price IS NULL THEN NULL
      WHEN x.price_model = 'per_unit' THEN round(x.old_regular_price, 2)
      WHEN x.delivered_units IS NULL THEN NULL
      ELSE round(x.old_regular_price * x.delivered_units, 2)
    END AS migration_old_derived_regular_price,
    -- Exact NEW-value rule in the proposed migration function.
    CASE
      WHEN NOT x.baseline_matches OR x.offer_id IS NULL
           OR x.proposed_action NOT IN ('CLEAR_PRICE', 'UPDATE_PRICE') THEN NULL
      WHEN x.proposed_regular_price IS NULL THEN NULL
      WHEN x.price_model = 'per_unit' THEN round(x.proposed_regular_price, 2)
      WHEN x.delivered_units IS NULL THEN NULL
      ELSE round(x.proposed_regular_price * x.delivered_units, 2)
    END AS migration_new_derived_regular_price,
    -- Exact calculation in the currently live service trigger.
    CASE
      WHEN NOT x.baseline_matches OR x.offer_id IS NULL
           OR x.proposed_action NOT IN ('CLEAR_PRICE', 'UPDATE_PRICE') THEN NULL
      WHEN x.proposed_regular_price IS NULL THEN NULL
      WHEN lower(coalesce(x.live_unit_type, '')) = 'unit'
           AND x.delivered_units IS NOT NULL AND x.delivered_units > 1
        THEN round(x.proposed_regular_price * x.delivered_units, 2)
      WHEN lower(coalesce(x.live_unit_type, '')) = 'unit'
           AND x.price_model = 'per_unit'
        THEN round(x.proposed_regular_price, 2)
      WHEN lower(coalesce(x.live_unit_type, '')) <> 'unit'
           AND x.delivered_units IS NOT NULL
        THEN round(x.proposed_regular_price * x.delivered_units, 2)
      WHEN lower(coalesce(x.live_unit_type, '')) <> 'unit'
           AND x.price_model = 'per_unit'
        THEN round(x.proposed_regular_price, 2)
      ELSE NULL
    END AS live_trigger_computed_regular_price
  FROM inputs x
)
SELECT
  d.service_id, d.business_id, d.qa_verdict, d.live_service_name,
  d.proposed_action, d.old_regular_price AS baseline_regular_price,
  d.live_service_regular_price, d.proposed_regular_price,
  d.old_unit_type AS baseline_unit_type, d.live_unit_type,
  d.old_service_area AS baseline_service_area, d.live_service_area, d.proposed_service_area,
  d.old_source_url AS baseline_source_url, d.live_source_url, d.proposed_source_url,
  d.baseline_updated_at, d.live_service_updated_at,
  d.baseline_matches, d.baseline_updated_at_matches,
  (SELECT count(*) FROM candidates) AS candidate_count,
  d.offer_id, d.offer_item_id, d.item_name, d.delivered_units, d.unit_price AS item_unit_price,
  d.current_offer_regular_price,
  CASE
    WHEN d.offer_id IS NULL OR NOT d.baseline_matches THEN d.current_offer_regular_price
    WHEN d.proposed_action = 'FIX_AREA_MAPPING' THEN d.current_offer_regular_price
    WHEN d.proposed_action NOT IN ('CLEAR_PRICE', 'UPDATE_PRICE') THEN d.current_offer_regular_price
    WHEN d.proposed_regular_price IS NULL THEN d.current_offer_regular_price
    WHEN d.is_package OR d.item_count <> 1 THEN d.current_offer_regular_price
    WHEN d.live_trigger_computed_regular_price IS NULL THEN d.current_offer_regular_price
    WHEN d.discount_price IS NOT NULL
         AND round(d.discount_price, 2) > d.live_trigger_computed_regular_price
      THEN d.current_offer_regular_price
    ELSE d.live_trigger_computed_regular_price
  END AS expected_regular_price_live_trigger,
  d.migration_old_derived_regular_price,
  d.migration_new_derived_regular_price,
  (d.live_trigger_computed_regular_price IS DISTINCT FROM d.migration_new_derived_regular_price
   AND d.proposed_action IN ('CLEAR_PRICE', 'UPDATE_PRICE')
   AND d.offer_id IS NOT NULL AND d.baseline_matches) AS projection_semantics_diverge,
  CASE
    WHEN d.offer_id IS NULL OR NOT d.baseline_matches THEN d.current_offer_regular_price
    WHEN d.proposed_action = 'FIX_AREA_MAPPING' THEN d.current_offer_regular_price
    WHEN d.proposed_action NOT IN ('CLEAR_PRICE', 'UPDATE_PRICE') THEN d.current_offer_regular_price
    WHEN d.is_package OR d.item_count <> 1 THEN d.current_offer_regular_price
    WHEN d.current_offer_regular_price IS NOT NULL
         AND (d.migration_old_derived_regular_price IS NULL
              OR round(d.current_offer_regular_price, 2) IS DISTINCT FROM d.migration_old_derived_regular_price)
      THEN d.current_offer_regular_price
    WHEN d.migration_new_derived_regular_price IS NOT NULL
         AND (d.discount_price IS NULL
              OR round(d.discount_price, 2) <= d.migration_new_derived_regular_price)
      THEN d.migration_new_derived_regular_price
    WHEN d.migration_new_derived_regular_price IS NOT NULL
      THEN d.current_offer_regular_price
    WHEN d.current_offer_regular_price IS NULL
         OR d.migration_old_derived_regular_price IS NOT NULL
      THEN NULL
    ELSE d.current_offer_regular_price
  END AS expected_regular_price_after_proposed_migration,
  CASE
    WHEN d.offer_id IS NULL THEN 'no linked Offer'
    WHEN NOT d.baseline_matches THEN 'baseline semantic fields changed; projection held at current value'
    WHEN d.proposed_action = 'FIX_AREA_MAPPING' THEN 'area-only candidate; no regular_price trigger'
    WHEN d.proposed_action NOT IN ('CLEAR_PRICE', 'UPDATE_PRICE') THEN 'no regular_price mutation proposed'
    WHEN d.is_package OR d.item_count <> 1 THEN 'package or multi-item; proposed migration skips'
    WHEN d.current_offer_regular_price IS NOT NULL
         AND (d.migration_old_derived_regular_price IS NULL
              OR round(d.current_offer_regular_price, 2) IS DISTINCT FROM d.migration_old_derived_regular_price)
      THEN 'preserved as source-owned/unknown by equality heuristic'
    WHEN d.migration_new_derived_regular_price IS NOT NULL
         AND d.discount_price IS NOT NULL
         AND round(d.discount_price, 2) > d.migration_new_derived_regular_price
      THEN 'derived update skipped by discount guard'
    WHEN d.migration_new_derived_regular_price IS NOT NULL THEN 'updated to proposed derived value'
    WHEN d.current_offer_regular_price IS NULL OR d.migration_old_derived_regular_price IS NOT NULL
      THEN 'old derived Offer regular cleared'
    ELSE 'preserved; old derived provenance unknown'
  END AS proposed_migration_decision,
  d.discount_price, d.is_active, d.is_package, d.price_model, d.item_count,
  d.offer_raw_text, d.offer_updated_at, d.item_updated_at, d.rationale
FROM derivations d
ORDER BY d.service_id, d.offer_id NULLS FIRST, d.offer_item_id NULLS FIRST;

COMMIT;
