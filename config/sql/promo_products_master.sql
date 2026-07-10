-- Retail skincare / shop catalog items separated from promo_offer_master.
-- Run in Supabase SQL Editor before migrate_skincare_products.py.

CREATE TABLE IF NOT EXISTS promo_products_master (
  product_id       BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
  business_id      BIGINT REFERENCES master_business_info(id) ON DELETE SET NULL,
  domain_name      TEXT NOT NULL DEFAULT '',

  product_name     TEXT NOT NULL DEFAULT '',
  brand_name       TEXT,

  regular_price            NUMERIC(10,2),
  discount_price       NUMERIC(10,2),

  source_url       TEXT NOT NULL DEFAULT '',
  promo_website_id BIGINT REFERENCES promo_website_staging(promo_website_id) ON DELETE SET NULL,
  offer_raw_text   TEXT,

  last_updated_at  TIMESTAMPTZ DEFAULT now(),
  created_at       TIMESTAMPTZ DEFAULT now()
);

CREATE INDEX IF NOT EXISTS idx_products_master_business ON promo_products_master(business_id);
CREATE INDEX IF NOT EXISTS idx_products_master_domain ON promo_products_master(domain_name);
CREATE INDEX IF NOT EXISTS idx_products_master_source_url ON promo_products_master(source_url);
CREATE INDEX IF NOT EXISTS idx_products_master_name ON promo_products_master(domain_name, product_name);

COMMENT ON TABLE promo_products_master IS
  'Retail skincare/catalog SKUs from shop/collections pages — not in-clinic treatment offers.';
