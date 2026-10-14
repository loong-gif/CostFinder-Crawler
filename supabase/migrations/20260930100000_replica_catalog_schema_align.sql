-- Replica-only schema alignment (ebamlycrvgtivuluipvn)
-- Brings catalog tables closer to production kdlpkjzcnbkjcvwsvlwn for data import.

BEGIN;

-- promo_offer_terms dependency
DO $$ BEGIN
  CREATE TYPE public.offer_purchase_unit_enum AS ENUM (
    'unit', 'vial', 'syringe', 'session', 'treatment', 'area', 'item', 'other'
  );
EXCEPTION WHEN duplicate_object THEN NULL;
END $$;

CREATE TABLE IF NOT EXISTS public.promo_offer_terms (
  offer_id bigint NOT NULL,
  membership_plan_id bigint,
  min_quantity numeric,
  min_unit public.offer_purchase_unit_enum,
  promo_code text,
  raw_terms text[] NOT NULL DEFAULT '{}'::text[],
  created_at timestamptz NOT NULL DEFAULT now(),
  updated_at timestamptz NOT NULL DEFAULT now(),
  is_new_customer_required boolean,
  is_returning_customer_required boolean,
  is_membership_required boolean,
  redemption_frequency text,
  requires_consultation boolean,
  is_not_combinable boolean,
  is_one_per_patient boolean,
  CONSTRAINT promo_offer_terms_pkey PRIMARY KEY (offer_id),
  CONSTRAINT promo_offer_terms_customer_flags_chk CHECK (
    NOT (is_new_customer_required IS TRUE AND is_returning_customer_required IS TRUE)
  ),
  CONSTRAINT promo_offer_terms_membership_plan_bool_chk CHECK (
    membership_plan_id IS NULL OR is_membership_required IS TRUE
  ),
  CONSTRAINT promo_offer_terms_min_purchase_quantity_chk CHECK (
    min_quantity IS NULL OR min_quantity > 0
  ),
  CONSTRAINT promo_offer_terms_offer_id_fkey FOREIGN KEY (offer_id)
    REFERENCES public.promo_offer_master(id) ON DELETE CASCADE,
  CONSTRAINT promo_offer_terms_membership_plan_id_fkey FOREIGN KEY (membership_plan_id)
    REFERENCES public.clinic_memberships(plan_id) ON DELETE SET NULL
);

CREATE INDEX IF NOT EXISTS idx_promo_offer_terms_membership_plan_id
  ON public.promo_offer_terms (membership_plan_id)
  WHERE membership_plan_id IS NOT NULL;

-- clinic_promotions
ALTER TABLE public.clinic_promotions
  ADD COLUMN IF NOT EXISTS screenshot text,
  ADD COLUMN IF NOT EXISTS requires_cf_verification boolean NOT NULL DEFAULT false;

DO $$ BEGIN
  IF EXISTS (
    SELECT 1 FROM information_schema.columns
    WHERE table_schema = 'public' AND table_name = 'clinic_promotions'
      AND column_name = 'promotion_content' AND udt_name = '_text'
  ) THEN
    ALTER TABLE public.clinic_promotions
      ALTER COLUMN promotion_content TYPE jsonb
      USING to_jsonb(promotion_content);
  END IF;
END $$;

-- clinic_memberships
ALTER TABLE public.clinic_memberships
  ADD COLUMN IF NOT EXISTS need_browser boolean NOT NULL DEFAULT false;

-- promo_offer_items
ALTER TABLE public.promo_offer_items
  ADD COLUMN IF NOT EXISTS item_name text;

DO $$ BEGIN
  IF EXISTS (
    SELECT 1 FROM information_schema.columns
    WHERE table_schema = 'public' AND table_name = 'promo_offer_items'
      AND column_name = 'quantity'
  ) AND NOT EXISTS (
    SELECT 1 FROM information_schema.columns
    WHERE table_schema = 'public' AND table_name = 'promo_offer_items'
      AND column_name = 'delivered_units'
  ) THEN
    ALTER TABLE public.promo_offer_items RENAME COLUMN quantity TO delivered_units;
  END IF;
END $$;

ALTER TABLE public.promo_offer_items
  ADD COLUMN IF NOT EXISTS delivered_units smallint;

-- promo_offer_master
ALTER TABLE public.promo_offer_master
  ADD COLUMN IF NOT EXISTS conditions text[],
  ADD COLUMN IF NOT EXISTS is_package boolean NOT NULL DEFAULT false;

COMMIT;
