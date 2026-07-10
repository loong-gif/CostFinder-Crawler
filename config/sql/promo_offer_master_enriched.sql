-- Join promo_offer_master with promo_membership_plans for read paths.
-- Run after promo_membership_plans and membership_plan_id FK exist.

CREATE OR REPLACE VIEW promo_offer_master_enriched AS
SELECT
  o.*,
 
  p.plan_name      AS plan_display_name,
  p.monthly_fee    AS plan_monthly_fee,
  p.annual_fee     AS plan_annual_fee,
  p.billing_period AS plan_billing_period,
  p.benefits       AS plan_benefits
FROM promo_offer_master o
LEFT JOIN promo_membership_plans p ON p.plan_id = o.membership_plan_id;

COMMENT ON VIEW promo_offer_master_enriched IS
  'Offer rows with membership plan fields joined via membership_plan_id.';
