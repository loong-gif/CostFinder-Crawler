# 抽取稳定性与准确性：决策记录

测试页面：https://www.laseraway.com/promotions/

## 已确定

1. 追问强度为深挖，决策逐条记录在本文件。
   理由：流程覆盖爬虫到生产入库的全部阶段，边界情况多，需要在实施前暴露风险。

2. 验收标准：一个页面同时满足以下三条，才算可以上传生产库。
   - 稳定：对同一份抓取结果连续两次运行 extract-pages、extract-offers、link-services，得到的 promotion、offer 和 item 完全一致（指纹与字段都相同）。
   - 准确：每条 offer 的价格、百分比、数量、条件、日期和 service 都能在 ego-lite 渲染中找到依据；页面上可以存储的优惠全部写入，不可存储的（例如纯赠品）一条也不写入。
   - 合规：AGENTS.md 的检查、batch-gate A–J 和 pytest 全部通过。
   理由：「同输入同输出」能把 LLM 的随机性和规则代码的错误分开判定。
3. 只登记生产库已覆盖城市里的 LaserAway 分店，从 discover-businesses 开始走完整流程，并真正写入生产库。
   理由：这样才是端到端测试，登记范围也和现有数据的覆盖范围一致。
4. 抓取时顺着卡片上的 offer terms 锚点链接，把对应的条款段落作为这张卡片的补充证据。条件、日期和数量可以从条款中取得，可追溯检查同时认可条款原文；offer 的来源和身份仍然是促销页。条款页里出现、但促销页上看不到的优惠不写入。
   理由：连锁品牌通常把条件放在单独的条款页上，只看促销页会让这类 offer 全部缺少条件。
5. 所有抓取统一使用 72.52.161.65 上的自托管 Firecrawl（WSL 内的 docker，地址 127.0.0.1:3002）。192.168.1.68 上的 Firecrawl 已经移除。
6. redis 在启动参数中加入 `--save ""`，关闭 RDB 快照。
   理由：Firecrawl 的任务记录保存在 nuq-postgres；redis 只放队列和限流这类临时数据，重启后不需要恢复。关闭快照后，不会再因为写盘失败而拒绝所有写入。
7. 只有当页面上全部 offer 属于同一个活动、截止日期也相同时，才填写页面级的 `campaign_start_date` / `campaign_end_date`；否则页面级日期留空，各条 offer 的活动日期写进它自己的 conditions。活动结束后，由 monitor 重新抓取时发现优惠已消失，再停用对应的 offer。
   理由：页面级截止日期会让 deactivate-expired 把同一页面上的长期 offer 一起停用。
8. 导航栏或通知区里的全站横幅优惠只在促销中心页（`/promotions/`、`/specials/` 这类路径）上写入，去重后保留一条。
   理由：横幅是品牌当前的主打优惠，条款完整；只在促销中心页写入，可以避免每个页面都重复抓到它。
9. 稳定性验收时清空该页面的 LLM 缓存，连续调用两次，结果必须一致。如果不一致，就把对应字段改为由代码从页面和条款原文中确定性地提取（百分比、单价、数量、日期），LLM 只负责切分卡片和匹配服务。正式运行时照常使用缓存。
   理由：带着缓存运行，两次结果必然相同，测不出稳定性。
10. 新规则只用 LaserAway 促销页测试效果，不对生产库现有的有效促销重新抽取。
11. 被 Cloudflare 拦截的站点由 ego-lite 渲染：Mac 上使用本机 ego-lite，服务器上使用 Windows 版 ego-lite（已安装并完成首次设置）。服务器上的 Firecrawl 不配置代理。
    理由：Apify 住宅代理实测仍被 LaserAway 的 Cloudflare 拦截（Cloudflare 识别的是无头浏览器特征），而 ego-lite 三次都拿到了完整页面。
12. 拦截页（Cloudflare 等反爬页面、403）不能证明优惠已经下架：有效 offer 保持不变，页面交给 ego-lite 补抓。只有 404/410 或明确下架才停用（monitor 无法访问时抛出 `page_not_accessible` 进入重试，只有确认 404 才停用）。

## 已查明的事实（2026-11-07）

- 生产库中没有任何 LaserAway 的商家、促销或 offer 记录。
- 连锁品牌在库中的现有做法：同一个促销 URL 为每个分店各写一条 `clinic_promotions`（例如 skinspirit.com 11 家分店、6 条有效促销）。
- 普通 HTTP 请求该页面返回 403；本机 ego-lite 渲染可以拿到完整内容。
- 72.52.161.65 上的 `firecrawl-api-1` 处于崩溃重启循环，已重启 2648 次，从 2026-11-06 15:45 UTC 容器重建后开始。原因是 compose 里 redis 以 `user: redis` 运行，但 `/data` 目录归 root 所有，RDB 快照写入失败（Permission denied）。redis 因此进入 MISCONF 状态，拒绝所有写操作，worker 退出，API 也随之重启。每天的 monitor 和 CrawlDue 在修复前都会失败。
- redis 已在 2026-11-07 03:31 UTC 按第 6 条重建（服务器备份：`docker-compose.yaml.bak.20261107_before_redis_nosave`），Firecrawl API 恢复工作。
- LaserAway 抓取实测（通过 72.52.161.65 的 Firecrawl）：
  - `fastMode` 返回 HTTP 200，`success: true`，正文却是 Cloudflare 拦截页（「Sorry, you have been blocked」）。`SelfHostedFirecrawl.scrape` 只检查 HTTP 状态，会把拦截页当作正常内容保存。
  - 浏览器路径同样拿到 403 拦截页（「Attention Required! | Cloudflare」），默认 30 秒超时内还会返回 `SCRAPE_TIMEOUT`（HTTP 408）。
  - 本机设置了 `HTTP_PROXY`，访问端口转发地址时必须绕过代理，否则本机代理会返回 502。
- 促销页上的 Black Friday「75% OFF LASER HAIR REMOVAL」只存在于导航下拉菜单和通知卡片中。ego-lite 渲染时，这些容器高度为 0（`submenu-column`、`notification-card`），鼠标悬停后子菜单容器的计算高度仍是 0；按现有渲染可见性规则，这张卡片不可见。页面顶部横幅只显示「The Black Friday Sale」。
- Google Maps scraper 在 72.52.161.65 上运行正常，经 SSH 端口转发 18081 → 8081 访问 `/api/v1/jobs` 返回 200。
- 3002 端口从 Mac 无法直接访问：`crawl` 与 `discover-pages` 在本机地址（`.env` 的 `FIRECRAWL_SELF_HOSTED_API_URL`，13002）不可达时自动建立 `ssh -L 13002:127.0.0.1:3002 firecrawl` 隧道，命令结束时关闭（`utils.firecrawl_client.firecrawl_tunnel`）。
- 页面内容：
  - 导航栏每个菜单里重复出现同一条 Black Friday 横幅「75% OFF LASER HAIR REMOVAL ǂ / Select packages」，共 9 次。
  - 正文有 10 张 First Time Specials 卡片，多数只有百分比，没有原价或现价；带价格的只有 Botox $10/unit、Xeomin $7/unit、Instant Glow $90。
  - 页面上只有标题和脚注符号（‡‡、^^、✢ 等）；适用条件、单位数量范围、新客户限制、活动日期写在单独的 `/offer-terms/` 页面，按锚点区分。
  - `/offer-terms/` 中的 Black Friday 条款写明 November 3, 2026 to December 7, 2026；同一页还列出若干 Black Friday 优惠（Clear + Brilliant 25%、RF Microneedling 25% 等），这些优惠没有出现在 `/promotions/` 页面上。
- 现有管线不会跟进页面上的条款链接。

- 72.52.161.65 的出口 IP 属于 Liquid Web 机房网段（AS32244），Firecrawl 没有配置代理。LaserAway 的 Cloudflare 对这个 IP 持续返回 403 拦截页，fastMode 和浏览器路径（120 秒超时）都一样。本机 ego-lite 可以正常拿到页面。

## 实施时确定的规则（2026-11-07）

以下规则都已写进代码并补了测试；它们针对的是 LaserAway 暴露出的问题，所有页面都适用。

13. 抓取（crawl、discover-pages、monitor 共用同一规则）：先用 Firecrawl fastMode（只走 HTTP 抓取）；请求失败、metadata 状态码 ≥ 400、正文为空或正文标题是拦截页标题时，改用 ego-lite 渲染（`utils/ego_browser.py`）。404/410 是页面下架的证据，不交给 ego。Firecrawl 的浏览器引擎不再使用。
    理由：monitor 主机的 Firecrawl 运行在 WSL1 上，容器内的 Chromium 自 2026-11-07 起会卡住，并把其他页面的内容当作当前请求的结果返回；换成新容器、去掉共享 IPC、改启动参数都无法恢复。
14. 连锁分店：discover-pages 每个网站只发现一次，结果复制给同站点的各家分店；review-pages 和 extract-pages 对同一页面的每家分店各保留一条决策、各写一行；内容相同的分店在 apply.sql 里合并成一个循环写入，循环内逐家核对。
15. 条款证据：crawl 记录每个条款锚点对应的卡片（链接之前最近的标题）；extract-pages 不把条款交给 LLM，卡片通过校验后由代码追加条款原文；可见性清单把条款页一起渲染，每一行、每个分句在促销页或条款页上可见即算可见。
16. 内容规范化：卡片行末的脚注符号去掉，「Book Here」「Subject to offer terms」这类按钮或引导语整行删除。
17. offer 字段的确定性来源：条件取自 offer 所在卡片的条件句原文；新客限制由原文和条件句判定；新客、会员、最低数量、服务部位按整张卡片核验，价格仍按 offer 原文核验；「Your First … $X」这类首次购买价属于促销价；「20 to 300 units」这类区间的下限就是最低购买量。
18. 组合判定：服务名本身含「+」（Clear + Brilliant）不拆；数字后的「+」（8+ Treatments）不是连接符；只有一个 item 的「互斥选项」按单项处理。
19. 卡片条款写明价格就是常规（批量）价格时，不作为促销写入（理由 `regular_price_not_promotion`）；同一来源中重复出现的 offer 不阻塞。
20. 单项 offer 的 item `unit_price` 按数据库触发器的同一规则计算（成交价 / 交付数量），重复运行不产生更新。
21. 同样的促销内容共用一份 offer LLM 回答（缓存按发给 LLM 的消息计算，不含来源 id）；LLM 请求（Gemini 与 OpenAI 兼容接口）的瞬时故障由连接库自动退避重试，命令外不需要重试循环。
22. 只在页头下拉菜单或通知卡片里出现的横幅优惠，算作「交互后可见」：深度渲染核验悬停页头的子菜单、点开通知开关，并以「文字区域中心点上最上层的元素就是这段文字」判定可见；促销中心页在 crawl 时把页头区域里带折扣措辞的横幅块补回 markdown（「Site-wide offers」小节），提取提示词要求列出全站横幅优惠，每条一次。
    理由：用户点开即可看到；这类横幅往往是品牌当前力度最大的活动。
23. LLM 引文里的脚注符号会被改写（字面 `\uXXXX`、形近字符，或 ⱽ 写成 ⁽v⁾），来源引文核对与卡片标题配对只比较 ASCII 字母数字与常用标点、带重音的拉丁字母，其余字符以及夹在符号中间的单个字母视为空白；只有符号的引文行不参与核对。
24. 卡片按标题在页面上首次出现的位置排序（促销中心页保留的已有卡片一并排序）；行末的「Subject to (offer) terms」引导语与单独一行的「Offer Terms」删除。
25. offer 原文跨越卡片中连续几行时，只保留到最后一行带金额或百分比的行；名称、数量、单价完全相同的 item 合并为一项；「package of N」属于条件措辞。
26. 连锁分店内容相同的促销 update、只有新建（其余不变）的 offer 写入同样合并为循环，循环内逐家核对计划时的状态。
27. 默认模型改为 gemini-2.5-flash-lite（`.env` 的 `LLM_MODEL`、`utils/gemini_client.GEMINI_MODEL`）。
    理由：同一页面上 2.5-flash-lite 与 3.5-flash-lite 各三次全新调用，规范化后抽取促销的结果六次完全一致；抽取 offer 各自三次一致，两模型之间只有 Black Friday 的 item 名称不同（「Laser Hair Removal Treatments (Small Area)」与「Laser Hair Removal」），都关联到 Laser Hair Removal 服务，效果没有变差。
28. monitor 中的连锁分店：多家商家共用同一来源页面、且地址各不相同时，是同一品牌的不同分店，每家各生成一份计划，页面只渲染、抽取一次；各家的 DO 块写进同一个 pending SQL 文件，任一分店被写入策略拦下时整页都不写入。地址相同或缺失时无法区分分店与重复登记，仍交人工审查（`source_business_ambiguous`）。
29. 发现阶段的站点链接来自 Firecrawl `/v2/map`（只读站点地图与页面链接，不经过浏览器引擎）；map 只拿到 3 条以内链接时，抓取首页取其中的链接（规则 13 的 fastMode 加 ego-lite）。不再使用 `/v1/crawl`。
30. monitor 主机上 ego-lite 由普通权限、运行在已登录桌面会话中的计划任务调用（ego-lite 拒绝管理员权限的连接）；WSL 中经 Windows 互操作调用 PATH 上的 `ego-browser.exe`。loong 账号断开远程桌面不影响，注销或服务器重启后需要重新登录一次。
31. 自托管 Firecrawl 串页期间（2026-11-06 15:45 至 2026-11-07 03:31 UTC）生成的 pending SQL 不执行：`pending-sql` 按文件名中的 UTC 时间戳排除（`utils.pending_sql.FIRECRAWL_OUTAGES`）。

## 验收结果（2026-11-07）

- 稳定：清空缓存后 extract-pages 连续三次调用 LLM，写入内容完全一致；extract-offers 连续三次调用 LLM，写入的 offer 与 item 完全一致（只有不写入的拒绝列表随 LLM 回答变化）。写入后重新读取快照再生成计划，34 条促销与 272 条 offer 全部是 unchanged。
- 准确：34 家分店各写入 1 条促销、8 条 offer（Botox $10/unit、Xeomin $7/unit、Clear + Brilliant 40%、RF Microneedling 40%、Thermage 40%、Instant Glow $90、IPL 40%、Laser Tattoo Removal 50%），全部标记新客专享，条件取自条款原文；9 张卡片在 ego-lite 渲染（促销页加条款页）中全部可见。没有写入的卡片：纯赠品（Free Treatments with Qualifying Purchase）、常规批量价格（CoolSculpting 50% Savings）。
- 合规：272 条 offer 全部激活、item 全部关联本店 service（新建 272 条 `clinic_services`）；AGENTS.md 的 blanket 检查 0 行；batch-gate 中没有 LaserAway 的失败行（失败的 7 行属于库中原有数据）；pytest 1259 passed。

## Black Friday 写入结果（2026-11-07）

- 34 条促销更新为 10 张卡片（新增「The Black Friday Sale / 75% OFF LASER HAIR REMOVAL / Select packages.」及条款原文），渲染核验 10/10 可见。
- 每家分店新增 1 条 offer：75% off，`is_new_customer_required=false`（活动不限新客），条件取自条款（2026-11-03 至 2026-12-07、小部位 8 或 10 次套餐、小部位清单、不可叠加、每人限一次等），item 关联新建的 Laser Hair Removal 服务。
- 生产库 LaserAway 共 34 条促销、306 条 offer，全部激活，item 全部关联本店 service；AGENTS.md blanket 检查 0 行；batch-gate 中没有 LaserAway 的失败行；写入后重新读取快照生成计划，促销与 offer 全部 unchanged；pytest 1265 passed。

## 洛杉矶 botox 主题搜索测试（2026-11-08）

规则与结果见 `docs/decisions-la-topic-search.md`。

## 整店商品目录页（2026-11-09）

simplicitylaser（商家 3100，`/collections/all`）与 smoothmd（商家 2932，`/collections/all-treatments`）的促销来源是整店商品目录，按标准流程处理：`recrawl-sources --business-id 3100 --business-id 2932` → `crawl` → `extract-pages plan`（`output/pipeline/extract_pages/20261109T112753Z/`）。

- smoothmd：9 张带售价的卡片在 ego-lite 渲染中全部可见，计划为更新促销 957（`row_differs`）。
- simplicitylaser：LLM 抽取的卡片写成「Original price: $89.00」，页面只有「Sale / $ 59.00 / $ 89.00」，措辞无法追溯，按规则拒绝本页（`no_validated_promotions`）；页面没有证明促销失效，库中已有的行保持不变。
