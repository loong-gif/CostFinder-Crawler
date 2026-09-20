BEGIN;

COMMENT ON COLUMN public.promo_offer_terms.min_quantity IS
    'Minimum quantity required to unlock the offer; distinct from promo_offer_items.delivered_units, which describes what the offer contains.';

COMMIT;
