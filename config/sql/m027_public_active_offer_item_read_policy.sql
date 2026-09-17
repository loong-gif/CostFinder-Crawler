BEGIN;

-- The public price-comparison page reads active offers with the anonymous
-- Supabase key. promo_offer_items has RLS enabled, so permit only the item
-- fields belonging to an active offer. This does not grant write access.
DROP POLICY IF EXISTS "anon_read_active_offer_items" ON public.promo_offer_items;

CREATE POLICY "anon_read_active_offer_items"
ON public.promo_offer_items
FOR SELECT
TO anon, authenticated
USING (
    EXISTS (
        SELECT 1
        FROM public.promo_offer_master AS offer
        WHERE offer.id = promo_offer_items.offer_id
          AND offer.is_active = TRUE
    )
);

COMMIT;
