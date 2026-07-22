-- M008: consumer marketplace freshness and anonymous outbound attribution.
--
-- Do not backfill last_verified_at from created_at or updated_at: neither proves
-- that the source page was successfully rechecked.

BEGIN;

ALTER TABLE promo_offer_master
    ADD COLUMN IF NOT EXISTS last_verified_at TIMESTAMPTZ;

CREATE INDEX IF NOT EXISTS idx_promo_offer_master_public_freshness
    ON promo_offer_master (last_verified_at DESC)
    WHERE status = 'active' AND last_verified_at IS NOT NULL;

CREATE TABLE IF NOT EXISTS public_outbound_clicks (
    id BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    offer_id BIGINT REFERENCES promo_offer_master(id) ON DELETE SET NULL,
    business_id BIGINT REFERENCES master_business_info(business_id) ON DELETE SET NULL,
    destination_kind TEXT NOT NULL CHECK (destination_kind IN ('offer_source', 'business_website')),
    entry_point TEXT NOT NULL,
    city TEXT,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS idx_public_outbound_clicks_offer_created
    ON public_outbound_clicks (offer_id, created_at DESC);
CREATE INDEX IF NOT EXISTS idx_public_outbound_clicks_business_created
    ON public_outbound_clicks (business_id, created_at DESC);

ALTER TABLE public_outbound_clicks ENABLE ROW LEVEL SECURITY;

DROP POLICY IF EXISTS "anon_insert_public_outbound_clicks" ON public_outbound_clicks;
CREATE POLICY "anon_insert_public_outbound_clicks"
    ON public_outbound_clicks
    FOR INSERT
    TO anon, authenticated
    WITH CHECK (
        (destination_kind = 'offer_source' AND offer_id IS NOT NULL)
        OR (destination_kind = 'business_website' AND business_id IS NOT NULL)
    );

REVOKE ALL ON public_outbound_clicks FROM anon, authenticated;
GRANT INSERT ON public_outbound_clicks TO anon, authenticated;
GRANT USAGE, SELECT ON SEQUENCE public_outbound_clicks_id_seq TO anon, authenticated;

COMMIT;
