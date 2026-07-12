# Plan Review Log: 高优先级生产安全与 Slack 通知修复
Act 1 (grill) complete — plan locked with the user. MAX_ROUNDS=5.


## Round 1 — Codex

Codex CLI 0.144.1 started a read-only review thread (`019f5442-6a7e-7cf1-994c-e7b5503b1170`), but the reviewer could not complete because the API WebSocket connection repeatedly failed with `tls handshake eof` while connecting to `wss://api.openai.com/v1/responses`. No critique or verdict was produced.

### Claude's response

Stopped the review loop rather than treating a transport failure as approval or proceeding to implementation without the required independent review. The plan remains locked and no business code was changed.


## Round 1 — Codex (proxy retry)

- Lifecycle split: code/migrations use both `status` and `lifecycle_status`, including forbidden `missing_once`/`stale_candidate`; fix with one canonical column and explicit backfill/cutover.
- Evidence is not wired end-to-end: change events emit empty `affected_segment_ids`; fix by persisting deterministic segments and linking every applied field to validated evidence.
- Transaction claim is unsupported: PostgREST writes business data, outbox, and monitor cursor separately; fix with one database RPC/direct SQL transaction covering all three.
- M002 is unsafe and non-repeatable: it drops `social_data_staging` then indexes it, unconditionally adds constraints, truncates data, and uses cascading FKs; fix with preflight checks, conditional constraints, explicit approval gates, and corrected ordering.
- Auto-apply gates are incomplete; direct write paths remain outside the audit/outbox flow. Fix with fail-closed gates and a complete writer inventory routed through the common mutation API.
- Outbox lease/claim concurrency, retry numbering, payload limits, redaction, and terminal transitions are underspecified; fix with a worker state machine and schema constraints.
- Hermes Block Kit support is unproven and AI tooling may have excessive terminal access; fix with verified capability detection, deterministic text fallback, and a restricted service identity/profile.
- Name-based Slack delivery is not durable; fix by storing the immutable channel ID.
- `InstagramTarget` must be an explicit dataclass/equivalent instantiable type and every real ingestion path needs regression coverage.

VERDICT: REVISE

### Claude's response

Accepted the material findings and added explicit writer inventory, canonical lifecycle cutover, evidence linkage, one-transaction RPC boundary, outbox lease/state-machine requirements, immutable Slack channel ID, Hermes least-privilege/fallback requirements, concrete Instagram typing, and safe M002 preflight/ordering constraints to PLAN.md.


## Round 2 — Codex

- Monitor concurrency remains underspecified; fix with per-monitor lease/advisory lock and compare-and-set cursor updates.
- Validator concurrency is unprotected; fix with atomic pending-to-applying transition, locking/lease, and unique applied-event key.
- Negative evidence for ended is undefined; fix with complete quality-validated snapshots and an explicit policy. The locked product decision remains one successful complete snapshot, not consecutive observations.
- Three-state lifecycle lacks a database invariant; fix with CHECK constraint, canonical reads/writes, and complete legacy backfill/cutover.
- Outbox immutability/redaction and provider-side duplicate handling are incomplete; fix with pre-persistence redaction, payload hash, immutable snapshot, idempotency marker and reconciliation.
- Migration ledger and cross-process lock are missing; fix with durable version/checksum/preflight/failure records and advisory lock.
- Restricted Hermes profile is not operationally defined; fix with isolated service identity/profile, minimal environment, disabled terminal tools, and privilege test.
- Business identity lacks enforced FK/ambiguity rejection; fix with canonical business_id constraints and needs_review on ambiguous mappings.
- Rollout flags lack crash/rollback semantics; fix with persisted versioned flags, atomic reads, kill switch and tested rollback.
- Legacy writers can still write old status; fix with compatibility boundary and deployment gate before cutover.

VERDICT: REVISE

### Claude's response

Accepted the findings and added explicit monitor/validator concurrency controls, complete negative-evidence quality rules, lifecycle CHECK/backfill compatibility, outbox redaction/hash/reconciliation, migration ledger/lock, isolated Hermes privilege boundaries, business identity FK/ambiguity handling, persisted rollout semantics, and legacy-writer cutover gates. Retained the user-locked single complete successful disappearance rule for ended rather than adding consecutive observations.


## Round 3 — Codex

- Monitor locking still lacked lease duration, owner, expiry recovery, and monotonic cursor contract; fix with atomic claim/renew/release RPC and CAS cursor update.
- Validator state transitions and unique constraints were not exact; fix with DDL CHECK/trigger, unique mutation key, stale-lease recovery, and transactional tests.
- Legacy writers remained a cutover risk; fix by naming every active entry point and making the deployment gate fail on direct legacy status writes/bypass RPC.
- Business identity chain remained incomplete; fix with explicit parent FKs, nullability rules, and ambiguity rejection tests.
- Outbox immutability/provider deduplication were not enforceable; fix with trigger/privilege design and persisted provider-message reconciliation.
- Migration ledger/runner protocol was not defined; fix with explicit ledger schema, advisory lock, checksum/preflight/failure/approval-token state machine.
- Hermes reuse and restricted identity requirements conflicted; fix by reusing the installed binary with an isolated profile/systemd unit and disabled tools.
- Rollout had no measurable promotion/abort criteria; fix with stage invariants, observation windows, kill-switch triggers, and rollback verification queries.

VERDICT: REVISE

### Claude's response

Added concrete contracts for monitor leases/CAS, validator DDL and transitions, named legacy writer cutover gates, business identity FKs and ambiguity handling, outbox trigger/privilege/reconciliation, migration ledger/runner protocol, isolated Hermes profile, and measurable rollout/kill-switch criteria.


## Round 4 — Codex

- Cursor monotonicity cannot use lexical check_id ordering; fix with an ordered `(created_at, sequence, check_id)` tuple and strict CAS.
- Validator uniqueness referenced nonexistent `source_event_id`; fix by using the repository's `change_event_id` and defining exact mutation-key derivation.
- Evidence business identity remained nullable/ambiguous; fix with explicit NOT NULL business_id FK and consistency enforcement across segment/offer/evidence.
- A file inventory cannot prevent future direct writers; fix with database role permissions and RPC-only writes for business tables.
- Hermes deployment wording still conflicted; fix by selecting the isolated worker profile/unit as the sole supported deployment model.
- Rollout needed minimum sample/coverage thresholds; fix with explicit coverage and zero-invariant promotion gates.

VERDICT: REVISE

### Claude's response

Added the ordered cursor tuple/CAS contract, aligned validator uniqueness to `change_event_id`, specified evidence business_id/FK consistency, added database-level RPC-only write permissions, made the isolated Hermes worker the sole supported deployment model, and set minimum rollout coverage gates.


## Round 5 — Codex

- Database permission isolation remains incomplete: active clients authenticate with `SUPABASE_SERVICE_ROLE_KEY`, which can bypass proposed application-role restrictions and still write tables directly. Fix by moving writers to a restricted role/API key, reserving service-role credentials for migrations, and adding a deployment test proving direct table writes fail.

VERDICT: REVISE

### Resolution

MAX_ROUNDS=5 reached without approval. This remains an explicit scope decision for the user: either include restricted Supabase writer credentials/role migration and direct-write denial tests in this phase, or accept that the RPC-only database boundary is not part of this phase and revise the safety claim accordingly. No business code was changed during Act 2.


### User resolution

User approved expanding this phase to include restricted Supabase writer credentials/API role, service-role isolation for migrations, and direct-write denial tests. The final plan now resolves the only Codex blocker. Codex review stopped at the configured MAX_ROUNDS=5; no code was written during the review acts.
