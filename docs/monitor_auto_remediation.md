# 监控自动处理

每日主任务和小时重试任务在处理完事件后由 `utils/monitor_remediation.py` 处理本轮结果，判定规则是
`utils/monitor_remediation_policy.py` 中的纯函数。监控运行是这些文件的唯一写入方，数据库行不因此改动。
决策记录见 [`decisions-monitor-auto-remediation.md`](decisions-monitor-auto-remediation.md)。

- 失败分类：抓取阶段的 `ego_scrape_failed`、`firecrawl_http_error`、`firecrawl_empty_markdown`、
  `ego_output_incomplete` 与处理阶段的 `page_not_accessible` 是站点一侧的失败；其余原因（图片截取不完整、
  需要新抓取、网络异常、限流等）是流水线自身的问题，每日自动重试，从不移出监控。
- 基础设施故障：抓取前检查 ego-lite（能打开任务空间）、OCR 服务（`/health`）与 Firecrawl
  （`/v0/health/liveness`，`utils/monitor_health.py`），任一失败则本轮全部失败记为基础设施故障。
  本轮抓取的页面中、上一次抓取成功的那部分有 30% 以上因同一原因失败时，该原因的失败记为基础设施故障。
  已在失败的来源（正在计数站点失败天数、抓取前已暂停小时重试、移出监控后复查）不进入分子和分母：
  服务中断让原本正常的来源失败，而长期无法访问的来源每天都被重新抓取，计入后会被永久当作基础设施故障，
  排除后它们照常累计天数。`extraction_credentials_rejected` 总是基础设施故障。基础设施故障不计入任何来源的天数。
- 站点失败天数（`state/site_failure_days.json`）：一个来源因站点原因失败的不同 UTC 日期，抓取成功或处理阶段拿到页面即清零。
  满 14 天时移出监控，写入 `state/monitor_removals.json` 的 `entries`：`{url, reason, removed_at, next_recheck_at}`，
  复查时间为 30 天后。
- 移出监控的来源不进入每日抓取和小时重试，到复查时间时加入每日抓取：成功即删除该项、恢复监控；
  站点原因失败则复查时间再推后 30 天；流水线原因或基础设施故障的失败不改变名单，下一次运行再复查。
- 审查状态的事件在来源每次按档期抓取成功后，用这次抓取的证据重新处理（不额外抓取）。重新处理后仍属三类人工审查
  （Source/Offer 身份、跨域跳到第三方、人工保护）的存档为「等待人工决定」，同一来源同一结论只存档一次，
  结论变化时再存档；不再需要人工审查时存档为「已解决」。
- 存档 `state/remediation_log.jsonl` 只追加，每个动作一行 JSON：`time`、`url`、`stage`（scrape / processing / review / run）、
  `reason`、`classification`（site / infra / human_review）、`action`（removed / restored / recheck_failed /
  awaiting_human / resolved / infra_fault）与 `evidence`（失败日期、复查时间、计划文件或故障计数）。
- 运行报告的 `remediation` 汇总本轮动作。飞书通知列出移出、恢复、复查未恢复、等待人工决定（最多列 2 个地址）、
  已解决的数量，基础设施故障单独一行，以及正在计数的站点失败来源和按原因分组的流水线问题（数量与最早一个持续的天数）。
