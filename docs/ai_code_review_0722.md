🌸 AI Code Review 🌸
   ─────────────────────────────────────────────────────────────────────────────────────────────

1. utils/change_driven_extractor.py
   Issue Score: 45.4

   🔍 Summary
   The file attempts to handle too many responsibilities in a single, flat module, creating mono
   lithic functions with extreme complexity, deep nesting, and rampant silent error suppression.
    This directly causes unmaintainable code and hides potential data corruption or injection is
   sues.
   
   💩 Key Issues (The Smelly Parts)
     • extract_and_upsert_check_pages (~202 lines): 8 parameters and complexity 17 show it crams
        page normalization, diff computation, and SQL generation into one function. Extract 
       _normalize_pages(), _compute_diffs(), and _upsert_pages().
     • apply_offer_actions (~159 lines): Complexity 27 from cascading conditionals. Split into a
        dispatcher with per-action-type handlers _apply_price_update(), _apply_availability_
       change().
     • _validate_single_offer_action (~101 lines): Combines validation, transformation, and erro
       r aggregation. Separate validation rules into small predicates and use an accumulator
       .
     • validate_change_event_for_auto_apply (~47 lines): 21 decision points. Replace nested if t
       rees with early returns and extracted boolean checks (e.g., _is_auto_applicable(event
       )).
     • Error Handling: 51.6% of caught exceptions are ignored (79 of 153). Silent except: pass b
       locks destroy debuggability and can hide SQL errors.
   
   🔧 Refactoring Suggestions
     1. Partition the file into extractors, validators, and builders modules.
     2. Replace 8‑param signatures with a PageChangeContext dataclass.
     3. Flatten apply_offer_actions using a dictionary mapping action types to processing functi
        ons.
     4. Refactor _validate_single_offer_action into a validate_rule_chain that collects errors v
        ia an ErrorCollector.
     5. Replace every except: pass with logger.exception() and re‑raise where the caller cannot 
        continue.
     6. Add docstrings to all public functions (current comment ratio 1.3%).
     7. Rename 27 naming violations to snake_case (PEP8).
     8. Write unit tests for each extracted function to lock in behaviour during refactoring.
   
   🔒 Security Concerns
     • SQL injection risk: build_offer_sql_statements and similar builders likely concatenate va
       lues into SQL; coupled with 79 ignored exceptions, failed parameter binding or inject
       ion will go unnoticed. Refactor immediately to use parameterized queries (e.g., ? pla
       ceholders) and validate all inputs.
     • Data integrity: Silent error suppression in upsert paths can leave partial changes withou
       t rollback. Ensure all DB operations run within transactions and re‑raise on failure.
   ─────────────────────────────────────────────────────────────────────────────────────────────

2. scripts/daily_instagram_promo_ingestion.py
   Issue Score: 40.5

   🔍 Summary
   Massive error suppression — 62% of all caught exceptions are silently ignored (except: pass),
    causing silent data corruption and making root-cause analysis impossible in a production dat
   a pipeline.
   
   💩 Key Issues (The Smelly Parts)
   
     • build_base_insert_payload (53-line function, CC=29): Monolithic payload construction with
        29 independent decision points. Extract discrete field resolvers (media_type, timest
       amp, hashtags) into private helpers; use a dictionary comprehension to map fields ins
       tead of branching.
   
     • insert_rows_with_fallback (59 lines, CC=13, nesting 4, params 6): Mixes retry logic, batc
       hing, and fallback mutation. Introduce a RetryableInsert context manager that accepts
        a payload dataclass and a db_config object, eliminating parameter explosion.
   
     • main (146 lines, CC=12, nesting 3): Orchestrates everything inline. Break into top-level 
       pipeline steps: targets = enrich(fetch()), posts = resolve_new_posts(targets), insert
       (posts). Each step becomes a pure function.
   
     • 44/71 caught exceptions ignored (62%): Every silent except: masks operational failures (n
       etwork, auth, DB). Replace all blind catch-all blocks with except Exception as e: log
       ger.exception("context"); raise. If an expected condition must be suppressed, wrap it
        in a specific exception type with inline justification comment.
   
     • 0.3% comment ratio: Zero docstrings or inline explanations. Add a module docstring descri
       bing data flow, and at minimum one-line docstrings for every function. Inline comment
       s must accompany any non-obvious transformation (e.g., date arithmetic, API mapping).
   
   🔧 Refactoring Suggestions
   
     1. Extract _parse_media_type(payload) and _format_instagram_date(ts) from build_base_insert
        _payload.
     2. Define a PromoPost dataclass; pass instances instead of scattered fields across insert_r
        ows_with_fallback.
     3. Split main into fetch_targets(), enrich_targets(), build_payloads(), and upsert_posts().
     4. Replace all except:/except Exception: with specific exception classes; log and re-raise.
     5. Add # noqa only where exception is intentionally swallowed, with a comment stating the r
        eason.
     6. Wrap duplicate post-filtering logic (dedupe_posts, resolve_only_posts_newer_than) into a
         single apply_filters(posts, rules) pipeline.
     7. Move the 16-parameter function (detected via avg params) to a configuration object; expo
        se only 1–2 parameters.
     8. Introduce a DatabaseConfig named tuple to replace scattered connection arguments.
   
   🔒 Security Concerns
   No direct SQL injection or credential leak patterns detected (likely using parameterized DB A
   PIs). However, silent exceptions may conceal permission errors or failed auth attempts, delay
   ing vulnerability detection. Ensure all database access uses query parameters and never f-str
   ing concatenation. Add alerts when ignored exceptions exceed a threshold.
   ─────────────────────────────────────────────────────────────────────────────────────────────

3. crawler/promo_site_crawler.py
   Issue Score: 39.0

   🔍 Summary
   The root cause of maintainability risk is the concentration of complex decision logic in mono
   lithic functions (e.g., score_page_segment with CC 29) that mix parsing, scoring, and filteri
   ng without decomposition, making any change error‑prone and untestable.
   
   💩 Key Issues (The Smelly Parts)
     • score_page_segment (L 84 lines, CC 29): Contains 29 independent branching paths due to nu
       merous if/elif conditions scoring segment properties. Extract each scoring dimension 
       into a dedicated method (e.g., _score_text_features, _score_position) and combine res
       ults in score_page_segment.
     • crawl_site (L 119, CC 22, depth 4): Tightly couples URL queue management, page fetching, 
       and segment processing. Split into _fetch_page(url) generator and _process_page(html,
        config) to flatten the loop and isolate error handling.
     • _extract_price_anchored_offer_segments (L 57, depth 5, CC 21): Deeply nested conditionals
        mask multiple extraction strategies. Apply guard clauses to exit early for each “no-
       match” scenario, and partition DOM/markdown parsing into _extract_price_offer_from_do
       m and _extract_price_offer_from_md.
     • _strip_cookie_consent_banners (L 56, CC 20): Complex heuristics for banner detection and 
       removal. Encapsulate detection rules in a BannerDetector class and removal in _remove
       _banner_node, reducing cognitive load.
     • Error handling: 17.6% of caught exceptions are silently ignored (likely in crawl loops). 
       Use logger.exception for all except clauses and re-raise only if the error is truly u
       nrecoverable.
   
   🔧 Refactoring Suggestions
     1. Decompose score_page_segment into _score_semantic, _score_visual, and combine via reduce
         (extract lines 10‑84).
     2. Extract a CrawlerState class holding URL queue and visited set to slim crawl_site’s stat
        e.
     3. Move markdown extraction from _extract_offer_segments_from_markdown to a MarkdownOfferEx
        tractor class with methods per block type.
     4. Replace nested if/else chains in _is_heading_or_context_line with a dictionary of regex 
        patterns and any().
     5. Add a @dataclass for the 7‑parameter function (max 7 params) to improve call site readab
        ility.
   
   🔒 Security Concerns
     • Ignoring all exceptions in crawl_site’s fetch loop (17.6% ignored) can hide TLS/connectio
       n errors, leading to incomplete site scans and potential data leakage from uncaught S
       SLError. Always log and continue, but never silence without at least a logger.error.
     • No injection risks visible from metrics alone, but the lack of comment/documentation (<1%
       ) makes security audits difficult; enforce docstrings on public functions to clarify 
       external input handling.
   ─────────────────────────────────────────────────────────────────────────────────────────────

4. scripts/daily_facebook_promo_ingestion.py
   Issue Score: 38.9

   🔍 Summary
   The root cause of fragility and untestability is a monolithic main function (144 lines) that 
   handles orchestration without separation of concerns, compounded by 51% of all caught excepti
   ons being silently ignored.
   
   💩 Key Issues (The Smelly Parts)
     • main (144 lines, likely L 580–724): Combines configuration loading, target fetching, post
        ingestion, and storage in one giant function. This makes testing impossible and hide
       s side‑effects.  
     • build_base_insert_payload (46 lines, complexity 28): Massive conditional nesting to assem
       ble an insertion payload. The function tries to handle every field variation inline. 
        
     • insert_rows_with_fallback (54 lines, complexity 13, nesting 4, 6 params): Mixes fallback 
       logic, retry mechanisms, and actual row insertion. The high parameter count obscures 
       intent.  
     • A function with 15 parameters (not shown in top‑10 list): Violates SRP; a single object s
       hould encapsulate the required configuration.  
     • Error Handling (33 of 64 caught exceptions ignored): Bare except: pass or empty exception
        blocks exist throughout the script, swallowing critical failures like API timeouts o
       r database errors.
   
   🔧 Refactoring Suggestions
     1. Split main into load_config(), gather_targets(), process_posts_for_actor(), and persist_
        results() – each ≤30 lines.  
     2. Extract field‑building from build_base_insert_payload into a PayloadBuilder class with o
        ne method per field group (e.g., add_media_fields()).  
     3. Replace the 15‑/6‑parameter functions with a single dataclass (e.g., InsertionContext) t
        o lower coupling.  
     4. Implement a global error‑handling decorator that logs full tracebacks and re‑raises non‑
        recoverable errors; remove all bare except.  
     5. Add module‑level docstring and per‑function docstrings (adopt Google‑style) to raise the
         comment ratio and clarify contracts.
   
   🔒 Security Concerns
   No direct vulnerability like SQL injection or token leakage is visible from the metrics alone
   . However, silently ignored exceptions could conceal authentication failures or permission er
   rors that expose API keys in logs. Ensure every exception is logged at warning level and sens
   itive config values are never printed.
   ─────────────────────────────────────────────────────────────────────────────────────────────

5. scripts/firecrawl_monitor_poll.py
   Issue Score: 37.6

   🔍 Summary
   The most critical issue is the excessive size and parameter bloat of _process_single_check an
   d process_monitor, indicating they handle multiple unrelated responsibilities. This leads to 
   high cyclomatic complexity (20), poor testability, and extreme difficulty in debugging or ext
   ending. The root cause is missing decomposition of business logic steps.
   
   💩 Key Issues (The Smelly Parts)
     • _process_single_check (L unknown): 126 lines, complexity 20, 12 parameters. It manages ba
       seline initialization, retries, result extraction, and domain inference all in one fu
       nction. Split into separate, focused functions: e.g., _create_check_context(), _execu
       te_with_retries(), _handle_check_result().
     • process_monitor (L unknown): 13 parameters, complexity 13, 97 lines. Parameter bloat obsc
       ures dependencies. Extract a MonitorConfig dataclass to bundle static settings, leavi
       ng only runtime parameters (check_context, monitor_id).
     • Naming violations (10 instances): Violates Python snake_case convention. Rename infer_dom
       ain_from_monitor to infer_domain_from_monitor (already snake) but other violations ne
       ed correction (e.g., likely camelCase names). Run pylint/flake8 with naming checks.
     • Missing documentation: Comment ratio 0.7%. Add docstrings to every public function explai
       ning purpose, parameters, returns, and raised exceptions.
   
   🔧 Refactoring Suggestions
     1. In _process_single_check, extract lines responsible for baseline initialization into _en
        sure_baseline(check, monitor_id, config).
     2. Bundle the 12 parameters into a CheckProcessingContext dataclass or namedtuple to reduce
         argument count.
     3. Extract retry logic (complexity 8) into a standalone _retry_with_backoff(operation, max_
        retries, delay) decorator or function.
     4. Split process_monitor into _resolve_target_urls(), _run_checks_for_monitor(), and _persi
        st_results().
     5. Remove duplication (7.4%) by creating a shared _fetch_page_and_extract_data(url, options
        ) function used by both duplicated code blocks.
     6. Add a LoggerAdapter to eliminate repetitive logger.debug(..., extra={...}) patterns.
   
   🔒 Security Concerns
   No direct security vulnerabilities detected. However, 11.4% error-ignored rate could silently
    suppress failures in network calls or parsing, potentially masking injection points. Wrap al
   l except: bare clauses with specific exception types and log critical errors. Ensure any user
   -supplied URLs passed to Firecrawl are validated/sanitized against SSRF.