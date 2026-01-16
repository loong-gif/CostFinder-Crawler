**CostFinder Production Workflow**

**Executive Summary**

This document defines the CostFinder production workflow for collecting, processing, and validating promotional data from medical spas and aesthetic clinics. The system prioritizes comprehensive raw data storage across staging tables, enabling debugging, reprocessing, and scalability while leveraging Python for intelligent processing and SQL for persistent, queryable storage. All PostgreSQL tables use lowercase naming conventions with underscores.

---

**Architecture Overview**

The workflow consists of five sequential phases:

1\.  	**Phase 1: Acquire Target Businesses** \- Crawler → Filter → Production

2\. 	**Phase 2: Website & Social Enrichment** \- Crawler → Enrich → Production

3\.  	**Phase 3: Master Consolidation & Promo Crawl** \- Join → Multi-path Crawlers

4\. 	**Phase 4: Data Structuring** \- Python GPT Processing → Production

5\.  	**Phase 5: Quality Assurance** \- Validation & Search Cross-reference

**Responsibility Distribution:**

·        **Crawler:** Data acquisition, raw output to staging tables

·        **Python:** Filtering, enrichment, processing, GPT structuring, joins

·        **SQL:** Persistent storage (raw & processed), queryability, scalability

---

**Phase 1: Acquire Target Businesses**

**Objective**

Generate a filtered list of qualified businesses from Google Maps API using relevance and review/score criteria.

**Process**

1\.  	**Crawler** scrapes Google Maps for businesses in target categories (Medspa, Plastic Surgery Clinic, Aesthetician, Skin Care Clinic)

2\. 	Output saved to **business\_list\_staging** (staging table) with complete raw data

3\.  	**Python** applies filtering logic:

o   Relevance: Keywords (Skin Care, Spa, Dermatologist, Beauty salon)

o   Review Count & Score:

§  Criteria 1: review\_count \>= 100 AND score \>= 4.0

§  Criteria 2: 50 \<= review\_count \< 100 AND score \>= 4.8

4\. 	**Python** loads qualified records to **business\_list\_production** (production table)

**Table Schemas**

**Staging: business\_list\_staging**

| Column | Definition |
| :---- | :---- |
| business\_id | UUID, Primary Key |
| name | text |
| address | text |
| website | In the format of ‘alluraderm.com’ |
| review\_count | integer |
| score | float |
| category | text (e.g., "Medspa", "Plastic Surgery Clinic") |
| raw\_json | jsonb (Full Google Maps API response) |
| crawl\_timestamp | timestamp |

 

**Responsibility:**

·        Crawler: Populate all columns from Google Maps API

**Production: business\_list\_production**

| Column | Definition |
| :---- | :---- |
| business\_id | UUID, Primary Key |
| name | text |
| address | text |
| website | text |
| review\_count | integer |
| score | float |
| category | text |
| filter\_passed | boolean |
| updated\_at | timestamp |
| created\_at | timestamp |

 **Responsibility:**

·        Python: Insert filtered records

·        SQL: Serve as input to Phase 2

---

**Phase 2: Website & Social Enrichment**

**Objective**

Clean website URLs and enrich business data with social media account information.

**Process**

1\.  	**Crawler** preprocesses websites (URL cleaning, social media detection) and fetches social account details:

o   If found on website: Crawl account details from the site

o   If not found: Search business name via Search API (Instagram, Facebook) and use first match

2\. 	Output saved to **social\_data\_staging** (staging table) with raw URLs and responses

3\.  	**Python** performs enrichment:

o   Extract clean domain from URLs

o   Normalize website columns

o   Flag special processing cases (Vagaro, Linktree, platform domains, location-based URLs, image-heavy content)

4\. 	**Python** loads enriched records to **enriched\_business\_info** (production table)

**Table Schemas**

**Staging: social\_data\_staging**

| Column | Definition |
| :---- | :---- |
| social\_data\_id | UUID, Primary Key |
| business\_id | UUID, Foreign Key → business\_list |
| website\_raw | text |
| social\_accounts\_raw | jsonb (Full crawl response with account handles, URLs) |
| crawl\_source | text ("website" or "search") |
| crawl\_timestamp | timestamp |

 **Responsibility:**

·        Crawler: Populate website and social account data

·        Python: Read and enrich

**Production: social\_data\_production**

| Column | Definition |
| :---- | :---- |
| business\_id | UUID, Primary Key |
| website\_clean | text (Extracted domain, normalized) |
| facebook\_url | text, nullable |
| instagram\_url | text, nullable |
| process\_flag | text, nullable (e.g., "vagaro", "linktree") |
| updated\_at | timestamp |
| created\_at | timestamp |

 

**Responsibility:**

·        Python: Insert cleaned and enriched records

·        SQL: Serve as input to Phase 3 master join

---

**Phase 3: Master Consolidation & Promo Crawl**

**Objective**

Create unified business master table and initiate multi-path promotional data collection.

**Process**

1\.      **Python** performs JOIN:

o   Combines business\_list\_production (Phase 1\) \+ social\_data\_production(Phase 2\)

o   Creates single source of truth with all business metadata

o   Inserts into **master\_business\_info** (master table)

2\.     **Crawlers** (3 parallel paths) begin promotional data collection from each business:

o   **Path A (Google Ads):** Crawler queries Google Ads Transparency Center, extracts promotion landing page URLs

o   **Path B (Email):** Crawler subscribes to business email lists, extracts newsletters/drip campaign content

o   **Path C (Websites):** Crawler identifies and scrapes relevant subpages (e.g., /service, /promotion, /pricing, /specials)

3\.      Raw promotional data saved to path-specific staging tables:

o   **promo\_google\_ads\_staging** (Google Ads)

o   **promo\_email\_staging** (Email)

o   **promo\_website\_staging** (Website)

**Table Schemas**

**Master: master\_business\_info**

| Column | Definition |
| :---- | :---- |
| business\_id | UUID, Primary Key |
| name | text |
| address | text |
| city | text |
| website\_clean | text |
| facebook\_url | text, nullable |
| instagram\_url | text, nullable |
| review\_count | integer |
| score | float |
| process\_flag | text, nullable |
| updated\_at | timestamp |
| created\_at | timestamp |

 

**Responsibility:**

·        Python: Create via JOIN, insert records

·        SQL: Serve as reference for promo crawlers and Phase 4

**Staging: promo\_google\_ads\_staging (Google Ads)**

| Column | Definition |
| :---- | :---- |
| promo\_google\_ads\_id | UUID, Primary Key |
| business\_id | UUID, Foreign Key → master\_business\_info |
| ad\_url | text |
| promo\_text\_raw | text |
| created\_at | timestamp |

 

**Responsibility:**

·        Crawler (Path A): Populate from Google Ads Transparency Center

·        Python: Read for structuring in Phase 4

**Staging: promo\_email\_staging  (Email)**

| Column | Definition |
| :---- | :---- |
| promo\_email\_id | UUID, Primary Key |
| business\_id | UUID, Foreign Key → master\_business\_info |
| email\_content | text |
| created\_at | timestamp |

 

**Responsibility:**

·        Crawler (Path B): Populate from email subscriptions

·        Python: Read for structuring in Phase 4

**Staging: promo\_website\_staging** **(Website)**

| Column | Definition |
| :---- | :---- |
| promo\_website\_id | UUID, Primary Key |
| business\_id | UUID, Foreign Key → master\_business\_info |
| subpage\_url | text |
| page\_content | text |
| crawl\_timestamp | timestamp |

 

**Responsibility:**

·        Crawler (Path C): Populate from website subpage crawls

·        Python: Read for structuring in Phase 4

---

**Phase 4: Data Structuring**

**Objective**

Convert raw promotional text from all paths into standardized, machine-readable JSON format.

**Process**

1\.  	**Python** reads all three staging tables (**promo\_website\_staging**  , **promo\_email\_staging**, **promo\_google\_ads\_staging** )

2\. 	**Python** uses GPT model to:

o   Extract specific promotional offers from raw text

o   Standardize into consistent JSON schema (offer\_id, service\_type, price, duration, description, etc.)

o   Handle null/missing values gracefully

3\.  	**Python** inserts structured offers into **structured\_promo\_offers** (production table)

**Table Schemas**

**Production: promo\_offers\_master**

| Column | Definition |
| :---- | :---- |
| offer\_id | UUID, Primary Key |
| business\_id | UUID, Foreign Key → master\_business\_info |
| promo\_path | text ("email", "google ads", or "website") |
| offer\_json | jsonb (Structured offer with service, price, etc.) |
| category | neurotoxins |
| subcategory | neurotoxin |
| service | botox |
| price | text, nullable |
| updated\_at | timestamp |
| created\_at | timestamp |

 

**Responsibility:**

·        Python: Read staging tables, apply GPT structuring, insert to production

·        SQL: Store structured data for QA phase

---

**Phase 5: Quality Assurance**

**Objective**

Verify accuracy and completeness of extracted offers through automated and manual review.

**Process**

1\.      **Automated QA:**

o   Read promo\_offers\_master

o   Cross-reference against Google Search for "\[Service Type\] \[City\]" promotions

o   Calculate match score based on relevance/similarity

o   Flag mismatches

2\.     **Manual QA Triggers:**

o   Top 5 businesses by review\_count in master\_business\_info

o   Businesses with review\_count \>= 100

o   Businesses on pre-defined whitelist (enterprise/chain spas)

3\.      **Results stored in qa\_results** tracking table

**Table Schemas**

**Production: qa\_results**

| Column | Definition |
| :---- | :---- |
| qa\_result\_id | UUID, Primary Key |
| offer\_id | UUID, Foreign Key → structured\_promo\_offers |
| business\_id | UUID, Foreign Key → master\_business\_info |
| google\_search\_query | text |
| search\_match\_score | float (0.0 to 1.0) |
| manual\_review\_needed | boolean |
| qa\_status | text ("pass", "fail", "review") |
| qa\_timestamp | timestamp |
| notes | text, nullable |

 

**Responsibility:**

·        Python: Execute automated QA, populate results

·        Human: Manual review for flagged records

·        SQL: Store and track QA audit trail

