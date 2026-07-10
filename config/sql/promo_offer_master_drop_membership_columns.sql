-- Drop redundant membership columns from promo_offer_master.
-- Prerequisite: migrate_offer_membership_fk.py completed; pipeline no longer writes these columns.
-- Recreate promo_offer_master_enriched after drop (o.* shape changes).

DROP VIEW IF EXISTS promo_offer_master_enriched;

ALTER TABLE promo_offer_master
  DROP COLUMN IF EXISTS membership_name,
  DROP COLUMN IF EXISTS membership_price,
  DROP COLUMN IF EXISTS billing_period;

CREATE OR REPLACE VIEW promo_offer_master_enriched AS
SELECT
  o.*,
  p.tier_name      AS plan_tier_name,
  p.plan_name      AS plan_display_name,
  p.monthly_fee    AS plan_monthly_fee,
  p.annual_fee     AS plan_annual_fee,
  p.billing_period AS plan_billing_period,
  p.benefits       AS plan_benefits
FROM promo_offer_master o
LEFT JOIN promo_membership_plans p ON p.plan_id = o.membership_plan_id;
