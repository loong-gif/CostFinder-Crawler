# CostFinder 数据流（规范快照）

> 表名以 `utils/schema_contract.py` 为准。会员 canonical：`clinic_memberships`。

```mermaid
graph LR
    classDef database_top fill:#ececff,stroke:#9370db,stroke-width:2px;
    classDef database fill:#d5e8d4,stroke:#82b366,stroke-width:1px;
    classDef database_crawl fill:#e8e8ff,stroke:#666699,stroke-width:2px;
    classDef crawl_proc fill:#dae8fc,stroke:#6c8ebf,stroke-width:2px;
    classDef process fill:#dae8fc,stroke:#6c8ebf,stroke-width:1px;
    classDef relation_step fill:#fff2cc,stroke:#d6b656,stroke-width:2px,stroke-dasharray: 5 5;

    Master_Biz[("master_business_info")]:::database_top

    subgraph Crawler_Layer ["爬虫层 (Firecrawl 通用流水线)"]
        direction TB
        C_Search["Firecrawl Search"]:::crawl_proc
        C_Raw[("firecrawl_search_raw")]:::database_crawl
        C_Scrape["Firecrawl Scrape<br>onlyMainContent=true"]:::crawl_proc
        C_Scrape_Raw[("firecrawl_scrape_raw")]:::database_crawl
        C_Search --> C_Raw
        C_Raw --> |"筛选有价目标url"| C_Scrape
        C_Scrape --> C_Scrape_Raw
    end

    subgraph AI_Extraction ["AI 提取层 (应用层)"]
        DB_Memb[("clinic_memberships")]:::database
        Proc_Serv["service_extraction_schema"]:::process
        DB_Serv[("clinic_services")]:::database
        Proc_Promo["promotion_extraction_schema"]:::process
        DB_Promo[("clinic_promotions")]:::database
        Proc_Offer["offer_extraction_schema"]:::process
        DB_Master[("promo_offer_master")]:::database
        DB_Items[("promo_offer_items")]:::database
        Match_Service["字段匹配 → service_id"]:::relation_step
        Match_Memb["字段匹配 → membership_plan_id"]:::relation_step
    end

    Master_Biz --> C_Search

    C_Raw -.-> |"1.1 会员数据"| DB_Memb
    C_Raw -.-> |"1.2 服务数据"| Proc_Serv
    Proc_Serv --> DB_Serv

    DB_Serv --> |"推进至促销阶段"| Proc_Promo
    C_Scrape_Raw -.-> |"2.2 促销数据"| Proc_Promo
    Proc_Promo --> DB_Promo
    DB_Promo --> Proc_Offer
    Proc_Offer --> DB_Master
    Proc_Offer --> DB_Items

    DB_Items ==> Match_Service
    DB_Serv -.-> Match_Service
    DB_Master ==> Match_Memb
    DB_Memb -.-> Match_Memb
```

## 节点说明

| 节点 | 写入时机 | 备注 |
| --- | --- | --- |
| `firecrawl_search_raw` | Search API 响应 | 价目/会员页 URL 发现；服务提取主输入 |
| `firecrawl_scrape_raw` | Scrape API 响应 | 仅 Search 命中含价格信号的 membership/promo URL |
| `clinic_memberships` | membership_extraction_schema | `plan_id` 供 master 关联 |
| `clinic_services` | service_extraction_schema | `service_id` 供 items 关联 |
| `clinic_promotions` | promotion_extraction_schema | 活动级，非 SKU |
| `promo_offer_master` | offer_extraction_schema | 价格/门槛/指纹 |
| `promo_offer_items` | offer_extraction_schema.items | 服务行 + 数量 |

## Legacy 提醒

- `promo_membership_plans`：旧表名，见 `LEGACY_MEMBERSHIP_TABLE`；新代码/Skill 以 `clinic_memberships` 为准。
- `promo_offer_master.service_id`：M009 前字段，已迁移到 `promo_offer_items.service_id`。
