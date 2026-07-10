-- Remaining steps after promo_membership_plans table exists (Task 2 + Task 7).

ALTER TABLE promo_offer_master
  ADD COLUMN IF NOT EXISTS membership_plan_id BIGINT REFERENCES promo_membership_plans(plan_id) ON DELETE SET NULL;

CREATE INDEX IF NOT EXISTS idx_offer_master_membership_plan
  ON promo_offer_master(membership_plan_id);

ALTER TABLE promo_website_staging
  ADD COLUMN IF NOT EXISTS is_membership_page BOOLEAN NOT NULL DEFAULT FALSE;

CREATE INDEX IF NOT EXISTS idx_staging_is_membership_page
  ON promo_website_staging(is_membership_page)
  WHERE is_membership_page = TRUE;
