BEGIN;

ALTER FUNCTION public.get_business_count_for_city(text) SET search_path = public;
ALTER FUNCTION public.get_business_category_counts() SET search_path = public;
ALTER FUNCTION public.set_updated_at() SET search_path = public;
ALTER FUNCTION public.set_business_profile_updated_at() SET search_path = public;
ALTER FUNCTION public.update_updated_at_column() SET search_path = public;

COMMIT;
