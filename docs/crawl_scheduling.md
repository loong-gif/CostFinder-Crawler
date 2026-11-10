# 抓取调度（新城市 / 老商户刷新 / 老城市增量发现）

状态全部在文件中（`state/crawl_schedule/`，可用 `COSTFINDER_CRAWL_STATE_DIR` 覆盖，已 gitignore），
**不新增任何数据库表、列或 migration**。原始抓取响应同样落在本地 `state/raw/`（`utils/raw_file_store.py`），
因为生产库没有 `firecrawl_scrape_raw` / `firecrawl_search_raw`。

| 文件 | 内容 |
| --- | --- |
| `schedule.json` | 以 `business_id` 为键：`tier`、`interval_days`、`last_crawled_at`、`next_crawl_at`、`consecutive_failures`、`pinned` 等 |
| `cities.json` | 以 `城市\|州` 为键：`status`(candidate/onboarding/active)、发现间隔、`parked_until` |
| `candidates.json` | 以 `google_place_id` 为键：低于过滤线的候选，用于识别越线 |
| `due.json` | `crawl_scheduler.py plan --apply` 生成的到期商户、URL 与 50 个一组的 bucket |
| `due_monitor.json` | 同一次 `plan --apply` 生成的监控格式候选配置（`costfinder_due_<日期>_NN`），不会自动并入静态配置 |

## 命令

```bash
python scripts/promo_pipeline.py crawl-schedule plan [--cap N] [--apply]   # 补种新商家与城市，列出今天到期的商户与 URL、到期城市
python scripts/promo_pipeline.py crawl-schedule record --results r.json [--apply]  # 回写抓取结果并调整 tier
python scripts/promo_pipeline.py discover-businesses --due [--max-cities N]  # 到期城市重新发现并登记
python scripts/promo_pipeline.py discover-businesses --city C --state S --out places.json
python scripts/promo_pipeline.py register-businesses --input places.json [--city C --state S]
python scripts/promo_pipeline.py select-city add|pick|pilot ...  # 新城市队列
python scripts/crawl_report.py                                  # 汇总
```

读库一律走 read-sql + read-snapshot：命令把只读查询写入 `output/pipeline/<命令>/<UTC 时间戳>/read.sql`，
用 Supabase CLI（`--linked`）执行得到 `snapshot.json`；也可用 `--snapshot` 指定已有快照。

- `crawl-schedule plan` 每次运行先把 `schedule.json` / `cities.json` 里还没有的商家和城市补种进去
  （`utils/crawl_schedule_seed.py`），新登记的商家在 apply.sql 执行后的下一次运行自动进入调度。
- `discover-businesses --due` 按 `cities.json` 选出到期的 active 城市（从未发现过的优先，每次最多 `--max-cities`），
  逐城抓取并立即登记；没有 `GMAPS_SCRAPER_URL` 时命令自己建立到抓取服务的 SSH 隧道。
- `register-businesses` 的动作（insert / promote / link / refresh）由过滤线判定、全部渲染；apply.sql 每条语句按
  `google_place_id` 判重，重复执行同一批地点不会重复登记。同一网站对应多家既有商家时按街道地址匹配；
  仍无法判定的地点写入运行目录的 `ambiguous.json`，候选标 `match_status=ambiguous`，下一轮发现时重新判定。
  apply.sql 经 Supabase MCP 执行；不删除、不降级已有商户。
- `select-city pick` 直接把选中的城市标为 onboarding；`select-city pilot CITY STATE` 从 `candidates.json`
  统计该城市通过过滤线且有网站的地点数，决定转 active 或搁置。

## 规则

- Tier：A 15 天（有活跃促销或会员）、B 30 天、C 90 天、D 连续失败 3 次后指数退避（上限 180 天）。
  变化升一档，连续 3 次无变化降一档；`pinned` 不调整；促销临近结束时不降档。下次抓取加 ±3 天抖动。
- 城市重新发现：商户 ≥25 家 14 天，≤4 家 60 天，其余 30 天。
- 过滤线在 `config/discovery_filters.py`（`review_count >= 50`、`score >= 4.0`、需有 website；已于 2026-10-15 由用户确认。现有低于线的商户只标记不删除）。
  首次发现即达标的新商户直接插入；低于线后上升的需连续 2 次达标才晋升。
- 新城市：每周一个，最多 2 个同时 onboarding；评分 = 3×同州活跃城市数(上限 5) + 2×注册用户数 + 预期供给/10 − 5×失败试点；
  试点达标商户 ≥15 才转 active，否则搁置 90 天。
- 主监控的页面队列由 `clinic_promotions` / `clinic_memberships` 的 source_url 动态生成，见 `docs/monitor_price_sync.md`「监控队列」；
  页面轮询节奏由 `utils/monitor_frequency.py` 的页面档位决定（促销页 7/15/30 天，会员页 60/90 天），实际运行时间取决于服务器 Windows 任务。

## 服务器部署（2026-10-15）

代码更新统一用 `deploy_monitor_price_sync.py`，部署不改动 `state/` 里的控制文件。`crawl-schedule plan` 读库走
read-snapshot，服务器上需要已登录并 `supabase link` 到生产项目的 Supabase CLI（`supabase/.temp/project-ref`）。

## 到期抓取闭环

```bash
python scripts/crawl_scheduler.py plan --apply        # 生成 due.json / due_monitor.json
# 只抓取：独立 state-dir，记录页面变化，不生成计划和待执行 SQL
python scripts/run_monitor_price_sync.py --config state/crawl_schedule/due_monitor.json \
    --state-dir state/due_monitor --capture-only
python scripts/crawl_record_from_monitor.py --state-dir state/due_monitor --due state/crawl_schedule/due.json \
    --monitors <本次运行的监控名,逗号分隔> --since <运行开始时间 ISO> --out state/crawl_schedule/results.json
python scripts/crawl_scheduler.py record --results state/crawl_schedule/results.json --apply
```

首次运行某个监控只建立基线（不产生变化事件）；`record` 之后 `next_crawl_at` 才会推进。

## 服务器上的运行（2026-10-15 起）

- 主机 `72.52.161.65`（SSH 别名 `firecrawl`，WSL Ubuntu），目录 `/home/loong/costfinder_monitor`。
- Windows 任务 `CostFinderCrawlDue`：每天 03:30（服务器太平洋时间）运行 `run_crawl_due.sh`，日志 `logs/crawl_due.log`。
  流程：`crawl_scheduler.py plan --apply --cap 30` → `monitor --capture-only`（独立 `state/due_monitor/`，
  只抓取，不生成计划和待执行 SQL，不发通知）→ `crawl_record_from_monitor.py` →
  `crawl_scheduler.py record --apply`。单次上限用环境变量 `CRAWL_DUE_CAP` 调整。
- 该任务只更新 `state/crawl_schedule/`，不会写数据库；每日监控（按页面 7/15/30 天档期抓取，见 `docs/monitor_price_sync.md`）与每小时重试任务不变。
- 停用：`schtasks /delete /tn CostFinderCrawlDue /f`。
- 抓取服务（google-maps-scraper v1.18.1）在 Firecrawl 服务器 `72.52.161.65` 上，监听 `0.0.0.0:8081`（服务器防火墙只放行项目 IP），
  Windows 任务 `CostFinderGmapsScraper`（登录时启动，程序与日志在 `C:\Users\loong\gmaps-scraper\`）。服务器直连谷歌，
  不需要代理。`discover-businesses` 与 `topic-search` 直接调用 `utils.service_endpoints` 中的地址（`http://72.52.161.65:8081`）。

## 用自部署 Firecrawl Map 发现新促销页 / 会员页

```bash
python scripts/promo_pipeline.py discover-pages --city "Newport Beach" --limit 15 --workers 3 \
    --batch-dir state/discovery/newport_beach     # 或 --business-id N（可重复）
```

流程（`utils/page_discovery_run.py` + `utils/promo_page_discovery.py`，经只读快照读取数据库，结果写入批次目录的 `discovered.json`）：

1. `POST {FIRECRAWL_SELF_HOSTED_API_URL}/v1/map` 取站点链接；只返回首页时用首页渲染后的链接兜底。
2. 路径按**整词**匹配（special/promo/offer/deal/discount/package/membership/vip/rewards/black-friday/
   pricing 等，`ideal` 不会命中 `deal`），同站、非博客/标签/资源文件，`/es/` 等语言重复页去重，
   并排除监控列表与库里已有证据 URL 的页面。
3. 抓取候选页（markdown + rawHtml）并校验，**只有出现真实折扣或价格才算有效**：
   百分比折扣、金额折扣、买赠、划线原价/现价、起价/单价、月费，金额范围 5–20000 美元；
   `$0`、"Free consultation"、URL 里的 `%E2%84` 不算。
4. 证据行的数字必须出现在按页面 HTML 与 CSS 计算出的可见文本里（`utils/crawler_visibility.py`）；
   样式表取不到、CSS 无法判定、文本过少或脚本过重的页面标 `needs_review`，不直接算有效。

输出每页的 `status`：`valid` / `needs_review` / `rejected` / `scrape_failed`，
附 `kind`（promotion / membership / pricing）和证据行。不会自动写入监控列表或数据库。

### 促销中心页的下一跳（`--hops 1`，默认开启）

候选页被拒绝的原因是"页面本身没有折扣或价格"，且路径属于促销或会员类时，视为中心页。此时只取该页
**正文区域**（排除 header、nav、footer、aside、form）里的同站链接，再往下抓一跳并用同样的校验：

- 只跟路径里带促销或会员词的链接（special/promo/offer/deal/package/reward/membership/black-friday 等；
  `specialty`、`specialist` 不算）；泛化的 `/collections/*`、`/treatments/*`、博客、语言重复页、
  已监控或已入库的页面都不跟。
- `--max-hop-links`（默认 8）限制每个中心页跟几个链接，`--max-pages`（默认 24）限制每家商户总抓取页数。
- 结果里每页带 `hop`（0 或 1）和 `via`（来自哪个中心页）；汇总里有 `hub_pages`、`hop_pages`、`hop_valid`。
- `--hops 0` 关闭。

实测（Newport Beach 15 家、Oklahoma City 25 家）：共识别 41 个中心页，但没有一个中心页的正文里带
促销词链接，下一跳抓取为 0 页、新增有效页 0 个。原因：这些中心页要么是积分计划、活动报名、条款这类不含价格的
说明页，要么站点 Map 已经把子页面直接列出（作为顶层候选被校验），所以下一跳是冗余的；它主要对"Map 只返回首页、
靠首页链接兜底"的站点有意义。服务端内容里本来没有价格的页面（如 BioRevive 的 `/pages/deals`，等待渲染后
内容不变）下一跳也取不到价格。

已知限制：JS 渲染或屏蔽爬虫的站点 map 只返回首页，靠首页链接兜底，仍可能漏。

## 促销 / 会员抽取代码的结构（重构后）

统一入口 `python scripts/promo_pipeline.py <command> [...]`，只做分发，参数和 `--apply` 门槛与各脚本一致：

| 命令 | 调用 | 写库 |
|---|---|---|
| `discover` | `discover_new_promo_pages`（Map + 价格证据校验） | 否 |
| `map-discover` | `discover_clinic_promotions_firecrawl`（旧版 special/promo） | `--apply` |
| `extract` | `extract_clinic_promotions_from_scrape_raw` | `--apply` |
| `backfill` | `backfill_clinic_promotions_firecrawl_gemini` | `--apply` |
| `audit` | `audit_missing_business_promotions_firecrawl` | 仅 `--write-raw`（本地 raw 缓存） |
| `domain` | `run_domain_architecture_pipeline` | `--apply` |
| `quality` | `run_data_quality_audit` | `--apply`（需已审核计划） |

共享代码已下沉到 `utils/`，脚本之间不再互相 import 这条链：

- `utils/promo_extraction.py`：LLM 抽取、payload 与证据校验、条目合并（原在抽取脚本内，行为不变）。
- `utils/promo_paths.py`：`normalized_host`、`sanitized_output_path` 和三套**有意不同**的促销 URL 规则
  （`loose` / `strict` / 旧版 `special|promo` 子串，各有测试，按接受范围命名而不合并）。
- `utils/gemini_client.py`：`build_gemini_client`（此前在两个脚本里各有一份）。

旧名字仍可从原脚本导入（测试与其他脚本不受影响）。新增的 `utils/promo_page_discovery.py`
是更宽的候选页启发式，通过后仍需价格证据校验和上面的抽取。
