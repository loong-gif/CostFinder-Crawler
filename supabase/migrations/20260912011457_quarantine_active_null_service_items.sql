-- An active offer with an unresolved item cannot be presented as a saleable
-- catalogue offer. Preserve the source row, but keep it out of the active set
-- until an operator supplies an exact service mapping.
UPDATE public.promo_offer_master AS offer
SET is_active = false,
    updated_at = now()
WHERE offer.is_active
  AND EXISTS (
      SELECT 1
      FROM public.promo_offer_items AS item
      WHERE item.offer_id = offer.id
        AND item.service_id IS NULL
  );
