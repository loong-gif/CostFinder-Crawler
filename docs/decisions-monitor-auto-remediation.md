# 每日 Firecrawl 监控：问题自动处理

## 背景事实（2026-11-10 从监控主机读取）

- 最近一次完整运行 `state/runs/20261109_083737_851219_full_monitor.json`：`paused_retries` 134 个；来源状态 review 137、retryable 164、excluded 13；`review_queue.json` 448 条。
- 134 个暂停来源按阶段和最后原因：processing/page_not_accessible 66，processing/image_capture_incomplete 26，processing/fresh_scrape_required 14，processing/ConnectionError 8，scrape/ego_scrape_failed 7，scrape/firecrawl_http_error 3，recovery/request_rejected 3，recovery/unexpected_row_or_trigger_change 3，processing/image_tiles_missing 2，scrape/firecrawl_empty_markdown 1，recovery/rest_intermediate_price_state 1。
- `clarebellaokc.com/specials`：scrape 阶段连续 5 次 `ego_scrape_failed`，processing 阶段连续 3 次 `page_not_accessible`。
- `laseraway.com/bundles/laser-hair-removal-specials`：`Unterminated string starting at: line 1 column 128`。`utils/ego_browser.py` 的 `ego_scrape` 经 console 输出整页 HTML 的 JSON，`html` 字段从第 128 个字符开始且没有结束，说明输出行被截断；这是代码缺陷，按根因修复。

## 结论

1. 审查由监控流水线执行：强制重新抓取（ego-lite 渲染），再经监控自身的事件处理（LLM 提取加 `utils/monitor_review_policy.py`）重新判定，代码按结论处理。理由：已有规则要求证据足够时由 AI 判定、未确认的页面自动交给渲染核验；在监控主机上运行，不依赖额外会话。
2. 自动处理包含生产写入。写入只在本机（Mac）经 Supabase MCP 执行，执行 SQL 不需要 Claude 判断。监控主机不持有写权限，用户提供的访问令牌只读。理由：写权限只留在本机，监控主机被入侵也不能改生产数据。
3. 自动审查确认连续 14 天仍无法访问的来源移出监控，数据库行保持原样。理由：无法核验不等于活动已结束，停用会违反「失败不是移除证据」；无限重试每天消耗 ego-lite 渲染。
4. 现有 `review_queue.json` 的 448 条一并纳入自动审查与存档。理由：同属「只通知、无人处理」，不纳入则清单持续增长。
5. 存档写在监控主机 `state/remediation_log.jsonl`，飞书通知附每日处理计数。理由：与证据文件在一处，单一写入方，不增加外部依赖。

6. AGENTS.md 规定的三类人工审查（证据无法判定的 Source/Offer 身份、跨域跳转到第三方、人工保护）保留：自动处理只重新取证，能判定的按结论处理，仍不能判定的存档为「等待人工决定」，通知中单独列出。理由：这三类正是证据无法判定的情形，交给 LLM 判定等于猜测。
7. 基础设施故障与站点故障分开计数：运行前 ego-lite、OCR、Firecrawl 健康检查失败，或本轮抓取的来源中、上一次抓取成功的那部分有 30% 以上因同一原因失败，记为基础设施故障（比例只按上次成功的来源计算：服务中断让原本正常的来源失败，长期失败的来源每天都被重新抓取，计入分母会让站点故障被永久当作基础设施故障），不计入任何来源的 14 天，通知中单独一行。只有单个来源特有的失败计入 14 天。理由：避免一次服务中断把整批正常来源移出监控。
8. 移出监控的来源每 30 天重新抓取一次，成功后自动恢复监控。理由：网站拦截常是暂时的，每月一次的成本很低。
9. `ego_scrape` 输出被截断（`Unterminated string`）按根因修复，HTML 改经文件传回。理由：代码缺陷，不属于决策。

10. 本机定时写入由 Python MCP 客户端执行（`pending-sql` 的子命令，launchd 定时启动）：拉取监控主机的待执行 SQL，调用托管 Supabase MCP 的 `execute_sql` 逐个执行并标记已执行。可写令牌只存在本机钥匙串。理由：执行 SQL 不需要 Claude 判断；结果确定；写权限只在本机。
11. 只读访问令牌放在监控主机 `.env`，监控主机的读取改为经 Management API 执行与本机相同的 `read.sql`，代替 REST 整表读取。理由：两边读取合并为一套实现。

12. 「无法访问」只指站点一侧：抓取阶段的 `ego_scrape_failed`、`firecrawl_http_error`、`firecrawl_empty_markdown`、`ego_output_incomplete` 与处理阶段的 `page_not_accessible`。按日期计数：一个来源在不同日期因站点原因失败满 14 天（成功即清零，基础设施故障的日期不计）时移出监控。其他原因（图片截取不完整、需要新抓取、网络异常、限流）是流水线自身的问题，每日自动重试，从不移出监控，通知按原因列出数量和持续天数。理由：只有站点一侧的长期失败说明这个来源确实抓不到；流水线问题移出来源只会掩盖缺陷。
13. 移出监控的名单存在监控主机 `state/monitor_removals.json`（来源地址、移出时间、下次复查时间），每日抓取选择页面时排除未到复查时间的来源；复查抓取成功即删除该项。理由：不新增数据表；目前没有移出监控的机制，需要新建。
14. 审查状态的事件在来源每次按档期抓取后自动重新处理；重新处理仍属于三类人工审查的，存档为「等待人工决定」，同一来源同一原因只存档一次，结论变化时再存档。`extraction_credentials_rejected` 记为基础设施故障，不进入人工审查。理由：重新取证不额外抓取；凭据被拒是本系统的问题。
15. `retry_limits.json` 的 `recovery` 阶段已无代码写入，删除。理由：残留项混在暂停来源里，无法处理也不会消失。

16. ego-lite 脚本在打开网站之前拒绝定位、通知、摄像头等浏览器权限，不让网站弹出权限提示。用户授权：每轮运行前，监控检查自己的 ego-lite 任务空间（渲染槽位、标题抓取），已被交给用户的，用 `claimTaskSpace` 自动收回并回到空白页，在 `remediation_log.jsonl` 记一条基础设施故障。理由：权限提示会把任务空间交给用户，该槽位之后的渲染全部失败（2026-11-10 的运行中，107 个正常页面有 103 个因此失败）；新出现的提示类型也不能让槽位永久停摆。

17. 爬虫过滤解析外部 CSS 判断隐藏：抓取页面引用的样式表（`<link rel=stylesheet>`、`<style>`、`@import`），用 `tinycss2` 解析，用 `soupsieve`（BeautifulSoup）匹配选择器，按层叠规则（`!important`、选择器权重、出现顺序、行内样式）计算每个元素的 `display`、`visibility`、`opacity` 等隐藏相关属性，媒体查询按渲染宽度 1440 计算。样式表取不到、选择器无法判定、内容依赖 JavaScript 时，页面交给 ego-lite 渲染。理由：网站自定义类名的隐藏规则写在外部样式表里，只看行内样式和常见类名会把隐藏的旧促销当成可见。
18. 爬虫内容作为可见性证据：`crawler_first` 成为默认；过滤后的爬虫内容被 `utils/crawler_visibility` 判定完全可见时，页面只用爬虫内容处理，不渲染、不截图；只有爬虫无法判定的页面交给 ego-lite。AGENTS.md「爬取的 Markdown 不是可见性证据」相应改写。理由：大部分页面的促销是 HTML 中的普通文字，浏览器只在爬虫判断不了时才需要。
19. 每次渲染只截一次图：整页截图一次，供 OCR 的图块（a、b 两轮）从这张截图裁切，b 轮的 2 倍尺寸由裁切图放大得到；商品选项每个选项只截一次，2 倍图同样由放大得到。理由：两轮读取的交叉核对针对的是识别结果，不需要两次截图；每多一次截图，在监控主机上就多一次超时的机会。

## 待定问题

无。
