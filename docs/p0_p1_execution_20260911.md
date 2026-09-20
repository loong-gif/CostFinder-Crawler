# CostFinder P0/P1 execution report — 2026-09-11

## Completed changes

- Confirmed the consolidated authenticated INSERT policies on `claims`,
  `saved_deals`, and `messages` include the suspended-profile predicate.
- Removed direct client execution of `public.is_admin()` and retained the
  private helper only for RLS use. Trigger-only functions remain non-callable
  by client roles; intentional client RPCs were not bulk-revoked.
- Confirmed public Offer reads require `is_active = true` and
  `moderation_status = 'approved'`.
- Made the Python fingerprint producer match the database trigger exactly:
  source-parent key plus normalized raw text, hashed with SHA-1. The CSV
  importer and legacy repair planner now pass parent IDs, and the batch SQL
  resolves the database parent before locating the inserted Offer.
- Repaired 11 source-confirmed price rows. The two remaining IDs from the
  original 13-row list (`8442`, `8448`) were already correct free-benefit
  rows.
- Added source-backed active Offer data for promotion `162` (Skinvive), `149`
  (two September Jeuveau prices), and `217` (CO2 Laser, Letybo, and
  Microneedling). Existing current-source rows in `217` were reactivated and
  corrected from an inherited per-unit model to total-price treatments.
- Re-read all current membership sources and added qualifying member-only
  Offers for plans `317`, `449`, `450`, `488`, `489`, `490`, `521`, `527`,
  `528`, and `621`. Membership-wide benefits retain a NULL service mapping
  with an explicit reason instead of an unrelated atomic service.
- Added three source-backed atomic services and fixed three deterministic item
  mappings. Remaining active unmapped items have an explicit reason.
- Applied the quantity rule in the import path. Per-unit, package, and
  variable-count NULL quantities are now explicitly classified; ordinary
  source-backed default-one rows remain `1`.

## Final database QA

All values below are zero unless otherwise stated:

| Check | Result |
| --- | ---: |
| Fingerprint mismatches | 0 |
| Active duplicate mapped Offer/service rows | 0 |
| Cross-business service mappings | 0 |
| Invalid quantity (`<= 0`) | 0 |
| `discount_price > regular_price` | 0 |
| `discount_amount > regular_price` | 0 |
| Expired active promotions | 0 |
| Active Offers on expired promotions | 0 |
| Active unmapped items without a reason | 0 |
| Active NULL quantities without a quantity reason | 0 |

All current CHECK constraints on `promo_offer_master` and
`promo_offer_items` are validated. The target price-anomaly IDs all pass their
price relationship checks.

## Migration history scope

The twelve P0/P1 migration versions from `20260911121709` through
`20260911211055` are applied both locally and in the target project. The
repository and target also have an older, pre-existing migration-history gap;
it is outside this repair plan and was not papered over with fabricated
history entries.

## Security Advisor outcome

The final Advisor run has no mutable-search-path finding and no public
`is_admin()` finding. Remaining SECURITY DEFINER warnings are intentional
client RPCs with purpose-specific authorization (`admin_set_user_status`,
claim-management RPCs, account deletion, notification and email-preference
helpers) plus the anonymous unsubscribe-token endpoint. The separate Auth
setting for leaked-password protection remains disabled and is outside this
database-repair plan.

## Extraction verification outcomes

The live re-read surfaced more rows than the plan's 4 Promotion / 8 Membership
snapshot. `is_offer_extracted` is intentionally unchanged for every residual
row: it means extraction ran, not necessarily that it produced a qualifying
Offer.

### Current-source outcomes

- Promotion `149`: source headed `Specials | Sep'26`; two Jeuveau offers were
  backfilled and the stale July end date was reconciled without inventing an
  end day.
- Promotion `162`: one explicit Skinvive offer was backfilled.
- Promotion `217`: four explicit September offers were reconciled.
- Promotion `3573`: the current page is a regular-price filler page, not a
  qualifying promotion; this is `verified_no_offer`.
- Promotion `3566`: the current services page contains no current price or
  promotional comparison; this is `verified_no_offer`.
- Promotion `3585`: visual inspection found only a product image and an
  expired “valid through August 31” notice, with no readable offer amount;
  this is `verified_no_offer` for the current page.
- Membership plans `317`, `449`, `450`, `488`, `489`, `490`, `521`, `527`,
  `528`, and `621`: current pages supplied concrete member-only prices or
  discount rates; qualifying Offers were backfilled.
- Membership plan `303`: the current page retains the membership fee but no
  longer publishes the historical Botox rate; this is `verified_no_offer`.
- Membership plans `342` and `345`: the recorded source URL now returns 404;
  this is `verified_no_offer`.
- Membership plan `455`: the recorded plan no longer matches the current
  programs; this is `verified_no_offer` for that plan identity.
- Membership plan `653`: the historical Gold plan is absent from the current
  page; this is `verified_no_offer`.

### Verified historical, archive, or non-promotional Promotion rows

| IDs | Evidence-based classification | Front-end effect |
| --- | --- | --- |
| `2` | Current page presents a referral credit rather than a service Offer. | No fabricated public Offer is shown. |
| `57, 68, 110, 168, 626, 886, 887, 921, 3520` | Expired or historical campaign/article records. Current pages may advertise different campaigns, which are not attached to the old IDs. | No potentially expired Offer is shown. |
| `147` | Current page terms explicitly expire the listed specials in 2023. | No potentially expired Offer is shown. |
| `3504` | The current URL renders a clinic home page; embedded social content has no stable page-level price evidence. | No fabricated public Offer is shown. |
| `3522, 3525, 3527, 3528` | Archive/tag pages, not a current campaign. | No fabricated public Offer is shown. |
| `3562, 3566, 3573, 3579, 3581, 3582` | Current services or price-list pages contain regular pricing or no promotional comparison. | No fabricated public Offer is shown. |

Every row is now either backed by a current qualifying Offer or classified as
processed with no qualifying current Offer. No `is_offer_extracted` flag was
reversed merely because an Offer was not created.
