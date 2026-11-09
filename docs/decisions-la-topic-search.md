# 洛杉矶 botox 主题搜索测试：决策记录（2026-11-08）

抽取管线的通用规则见 `docs/decisions-extraction-stability.md`。

## 用户确定

- 发现方式：按城市、服务主题搜索促销页（`topic-search`），接入现有管线并按 LaserAway 的标准验收稳定性与准确性，另报告搜索发现的精度。
- 范围：Los Angeles 及 Beverly Hills、Santa Monica、West Hollywood、Sherman Oaks、Encino、Pasadena、Studio City、Brentwood、Silver Lake；地点城市不在这些城市内的诊所不计入。
- 诊所不在库中时按 Google Maps 结果注册并写入生产库。
- 搜索结果中的同站促销页直接作为候选页，只命中首页的诊所再做站内发现（`/v2/map` 与首页链接）。

## 发现与注册规则

- 云端 Firecrawl `/v2/search` 本月额度已用完（2026-11-18 重置），使用自托管 `/v1/search`。
- 判断「是不是目标区域的诊所」不维护过滤名单：搜索命中的网站域名与目标城市某个 Google Maps 地点的网站一致才算；地点类别明显不是医美诊所（健身、理发、公司总部等）时不算；Zocdoc 列入目录站。
- Google Maps 结果按 UTF-8 解码（scraper 响应头不带 charset，按 ISO-8859-1 解码会把 Aère 变成乱码）。
- 按网站链接已有商家时，城市也必须一致；同一网站、不同城市的是连锁品牌的另一家分店（例如 New Look Skin Center 的 Irvine 与 Encino）。
- 发现结果的 URL 合并路径中的重复斜杠（站点内链接 `oubeauty.com//specials` 与 `/specials` 是同一页面）；路径段含 `terms` 的条款页（`laseraway.com/offer-terms`）不作为促销来源。review-pages 读取早先的发现结果时按这两条规则复核。

## 促销与会员抽取规则

- 抓取结果里没有可用标题时，由 ego-lite 渲染页面取 `<title>` → og:title → 第一个 `<h1>`（`utils.ego_browser.fetch_page_title`）。
- 抽取卡片与来源复核卡片的配对：
  - 只有完整卡片内容经 markdown、脚注和引导语规范化后相同，才按页面重复布局去重；标题、金额或百分比相同不足以判定重复，服务与条件差异必须保留。
  - 抽取卡片优先按标题对应；无标题对应时，只有复核卡片原文能连续包含规范化后的服务名称和该抽取卡片的正文、并且金额与百分比证据唯一对应，才允许价格配对。
  - 抽取卡片的金额与百分比必须都在对应的复核卡片里；一张总卡片可以对应多项不同服务。重复去重按规范化后的完整抽取卡片内容判定，同价的不同服务、数量或条件都保留。
  - 同标题的复核卡片内容不同且无法按原文唯一配对，或价格证据不一致，整页进入复核。
- 可见性核验（`scripts/render_review_deep.js`）只按 PC 屏幕宽度 1440 渲染，与 monitor 相同；连接被断开或重置时重新导航一次。
- 会员费用与方案名称不在同一行（例如名称是标题、「$99/month」在下一行）时，费用由渲染布局证实：这类方案在首次计划中暂不写入，但保留名称、费用和来源清单，进入可见性分片；带清单重新计划时，可见性清单中「名称 $费用」核验可见即认可。
- 会员方案的来源清单写明只在某个地点提供（「Offered in Denver only」），且该地点不是本商家所在城市时，不写给本商家（理由 `membership_offered_only_elsewhere`）；快照的商家表带出 city。

## offer 与服务关联规则

- 单价 offer 在服务关联确定之前以未激活状态写入（触发器 `prevent_incomplete_offer_activation` 不允许激活），link-services 关联服务后由 extract-offers 重新激活：extract-offers 自动选取"未激活 offer 的 item 已全部关联、且关联晚于 offer 最近一次写入"的来源（`utils.offer_stage.snapshot_tables`）。
- LLM 从卡片后的条款段落取了 offer 原文时（gemini-2.5-flash-lite 与 gemini-2.5-flash 都会这样做），改用同一张卡片中条款之前、带有同一折扣数值的唯一标题行作为原文，条款作为条件，item 名称取原文的短标签；找不到唯一标题行时，含「Valid on purchases made」的条款段落不作为 offer 原文（`terms_paragraph_offer_text`）。
- 小写冠词开头（「a package of 8」）或含「is available」的 item 名称是句子成分（`clause_fragment`）。
- 同一来源中 item、价格字段与客户限制都相同的 offer 只保留条件最多的一条。
- 单个波浪号的删除线价格（`~$150~`）同样是原价。
- 「Facial Balancing」是填充剂注射，服务别名映射到 Dermal Filler。
- 服务枚举中没有对应值的项目（Emsella、腿部静脉治疗）保持未关联，不交给 LLM 选近似的枚举值。

## 依据现有规则作出的判定

- Los Feliz（4443）的「St. Patrick's Day Sale」页面仍在线、没有年份，按 AGENTS.md 写入，活动日期为空，下架后由 monitor 停用。
- LaserAway 洛杉矶三家分店（4439–4441）的来源页面是 `/savings` 与 `/bundles/laser-hair-removal-specials`。
- Le Rêve（4442）重复的 New Client BOTOX 卡片合并为一条 offer。
- batch-gate 检查条件（`utils/batch_gate_checks.py`）直接排除以下情形，不逐条登记：
  - C：类别内折扣（「all IV services」「all laser services」）关联到名称含该类别词的服务。
  - D：同一权益出现在同一商家的多个会员方案或促销中，按来源分组。
  - G：「买 N 送」类 offer（「买 2 送第 3 支折扣」及其二选一组合），折扣由多个单位共享，交付数量不是单一数值。

## 结果

- 搜索：10 个区域 × 3 条查询共 300 条命中、58 个网站；41 个网站对上目标城市诊所（62 个地点）；5 个网站的诊所在目标城市之外，12 个网站没有对应诊所（百科、厂商站、外州诊所等）。
- 搜索精度：命中中 33% 落在目标区域诊所网站上；搜索命中的同站页面 36 个，审核通过 19 个（53%），站内发现的页面 271 个，审核通过 77 个（28%）；写入的 17 个来源页面中 3 个直接来自搜索命中，其余来自对命中诊所的站内发现。
- 注册：38 家新商家（business_id 4419–4456）；9 个地点低于注册线（评论少于 50 或评分低于 4.0），7 个连锁分店因同网站在其他城市已有未关联的商家而不确定，1 个站点的 Google Maps 查询任务卡住未查。
- 发现与审核：310 条页面记录，审核后 70 个 URL、34 家商家进入抓取，抓取 70/70 成功；渲染核验 45 个页面，全部成功。
- 稳定性：清空 LLM 缓存后连续生成三次计划，140 个写入目标的动作、原因与全部写入字段与缓存计划完全一致。
- 生产库：22 条促销、25 条会员、82 条有效 offer；没有 offer 的 2 条促销是 Skin Spa 的推荐好友卡片（无价格）和 Los Feliz 的礼品卡促销（礼品卡不作为 offer）。AGENTS blanket 检查 0 行，batch-gate 通过。
