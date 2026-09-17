BEGIN;

-- M028 was already applied before these function attributes were added. Pin the
-- runtime search path on the live functions to prevent role-controlled schema
-- resolution and satisfy the Supabase security advisor.
ALTER FUNCTION public.fn_auto_fix_offer_prices() SET search_path = public;
ALTER FUNCTION public.normalize_promo_offer_item_unit_price() SET search_path = public;
ALTER FUNCTION public.sync_promo_offer_item_unit_prices() SET search_path = public;

COMMIT;
