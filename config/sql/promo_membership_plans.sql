-- Membership plans separated from promo_offer_master.
-- Run in Supabase SQL Editor before backfill_membership_plans.py.

-- ---------------------------------------------------------------------------
-- Task 1: promo_membership_plans
-- ---------------------------------------------------------------------------

CREATE TABLE IF NOT EXISTS promo_membership_plans (
  plan_id          BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
  business_id      BIGINT REFERENCES master_business_info(id) ON DELETE SET NULL,
  domain_name      TEXT NOT NULL DEFAULT '',

  tier_name        TEXT NOT NULL DEFAULT '',
  plan_name        TEXT NOT NULL DEFAULT '',

  monthly_fee      NUMERIC(10,2),
  annual_fee       NUMERIC(10,2),
  billing_period   TEXT NOT NULL DEFAULT 'monthly',

  benefits         JSONB NOT NULL DEFAULT '[]'::jsonb,

  source_url       TEXT NOT NULL DEFAULT '',
  promo_website_id BIGINT REFERENCES promo_website_staging(promo_website_id) ON DELETE SET NULL,
  crawl_timestamp  TIMESTAMPTZ,
  last_updated_at  TIMESTAMPTZ DEFAULT now(),
  created_at       TIMESTAMPTZ DEFAULT now()
);

CREATE INDEX IF NOT EXISTS idx_membership_plans_business ON promo_membership_plans(business_id);
CREATE INDEX IF NOT EXISTS idx_membership_plans_domain ON promo_membership_plans(domain_name);
CREATE INDEX IF NOT EXISTS idx_membership_plans_source_url ON promo_membership_plans(source_url);

COMMENT ON TABLE promo_membership_plans IS
  'Membership tier structure (fees, billing, non-priced benefits). Priced member offers link via promo_offer_master.membership_plan_id.';

-- ---------------------------------------------------------------------------
-- Task 2: link offers to membership plans
-- ---------------------------------------------------------------------------

ALTER TABLE promo_offer_master
  ADD COLUMN IF NOT EXISTS membership_plan_id BIGINT REFERENCES promo_membership_plans(plan_id) ON DELETE SET NULL;

CREATE INDEX IF NOT EXISTS idx_offer_master_membership_plan
  ON promo_offer_master(membership_plan_id);

-- ---------------------------------------------------------------------------
-- Task 7: mark membership pages in staging
-- ---------------------------------------------------------------------------

ALTER TABLE promo_website_staging
  ADD COLUMN IF NOT EXISTS is_membership_page BOOLEAN NOT NULL DEFAULT FALSE;

CREATE INDEX IF NOT EXISTS idx_staging_is_membership_page
  ON promo_website_staging(is_membership_page)
  WHERE is_membership_page = TRUE;
