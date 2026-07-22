BEGIN;

-- ---------------------------------------------------------------------------
-- claim_status enum (matches frontend/src/types/claim.ts)
-- ---------------------------------------------------------------------------
DO $$
BEGIN
  CREATE TYPE public.claim_status AS ENUM (
    'pending',
    'contacted',
    'booked',
    'completed',
    'cancelled',
    'expired'
  );
EXCEPTION
  WHEN duplicate_object THEN NULL;
END;
$$;

-- ---------------------------------------------------------------------------
-- claims table
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS public.claims (
  id uuid NOT NULL DEFAULT gen_random_uuid(),
  consumer_id uuid NOT NULL,
  deal_id bigint NOT NULL,
  business_id bigint NOT NULL,
  status public.claim_status NOT NULL DEFAULT 'pending'::public.claim_status,
  preferred_date date NULL,
  preferred_time text NULL,
  notes text NULL,
  business_response text NULL,
  responded_at timestamptz NULL,
  booked_date date NULL,
  booked_time text NULL,
  expires_at timestamptz NOT NULL DEFAULT (now() + interval '7 days'),
  created_at timestamptz NOT NULL DEFAULT now(),
  updated_at timestamptz NOT NULL DEFAULT now(),
  relayed_at timestamptz NULL,
  relayed_by text NULL,
  relay_method text NULL,
  CONSTRAINT claims_pkey PRIMARY KEY (id),
  CONSTRAINT fk_claims_consumer FOREIGN KEY (consumer_id)
    REFERENCES public.profiles (id) ON DELETE CASCADE,
  CONSTRAINT fk_claims_deal FOREIGN KEY (deal_id)
    REFERENCES public.promo_offer_master (id) ON DELETE CASCADE,
  CONSTRAINT fk_claims_business FOREIGN KEY (business_id)
    REFERENCES public.master_business_info (business_id) ON DELETE CASCADE,
  CONSTRAINT claims_notes_length CHECK (
    notes IS NULL OR char_length(notes) <= 1000
  ),
  CONSTRAINT claims_relay_method CHECK (
    relay_method IS NULL OR relay_method IN ('email', 'phone', 'manual')
  )
);

CREATE UNIQUE INDEX IF NOT EXISTS claims_active_unique
  ON public.claims USING btree (consumer_id, deal_id)
  WHERE status <> ALL (ARRAY['cancelled'::public.claim_status, 'expired'::public.claim_status]);

CREATE INDEX IF NOT EXISTS idx_claims_consumer_id
  ON public.claims USING btree (consumer_id);

CREATE INDEX IF NOT EXISTS idx_claims_deal_id
  ON public.claims USING btree (deal_id);

CREATE INDEX IF NOT EXISTS idx_claims_business_id
  ON public.claims USING btree (business_id);

CREATE INDEX IF NOT EXISTS claims_status_idx
  ON public.claims USING btree (status);

CREATE INDEX IF NOT EXISTS idx_claims_relay
  ON public.claims USING btree (relayed_at)
  WHERE relayed_at IS NULL;

DROP TRIGGER IF EXISTS trg_claims_updated_at ON public.claims;
CREATE TRIGGER trg_claims_updated_at
  BEFORE UPDATE ON public.claims
  FOR EACH ROW
  EXECUTE FUNCTION public.update_updated_at_column();

-- ---------------------------------------------------------------------------
-- RLS
-- ---------------------------------------------------------------------------
ALTER TABLE public.claims ENABLE ROW LEVEL SECURITY;

GRANT SELECT, INSERT, UPDATE ON public.claims TO authenticated;

DROP POLICY IF EXISTS "consumers_read_own_claims" ON public.claims;
CREATE POLICY "consumers_read_own_claims"
  ON public.claims FOR SELECT TO authenticated
  USING ((SELECT auth.uid()) = consumer_id);

DROP POLICY IF EXISTS "consumers_insert_own_claims" ON public.claims;
CREATE POLICY "consumers_insert_own_claims"
  ON public.claims FOR INSERT TO authenticated
  WITH CHECK ((SELECT auth.uid()) = consumer_id);

-- Business/admin policies require business_profiles / admin_profiles tables.
-- Add in a follow-up migration once those tables are deployed.

DO $$
BEGIN
  IF NOT EXISTS (
    SELECT 1 FROM pg_class
    WHERE oid = 'public.claims'::regclass AND relrowsecurity
  ) THEN
    RAISE EXCEPTION 'claims RLS must be enabled';
  END IF;
  IF NOT EXISTS (
    SELECT 1 FROM pg_indexes
    WHERE schemaname = 'public' AND indexname = 'claims_active_unique'
  ) THEN
    RAISE EXCEPTION 'claims_active_unique index is missing';
  END IF;
END;
$$;

COMMIT;
