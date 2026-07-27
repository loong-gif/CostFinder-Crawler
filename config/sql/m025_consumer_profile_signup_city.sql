BEGIN;

CREATE OR REPLACE FUNCTION public.handle_new_consumer()
RETURNS trigger
LANGUAGE plpgsql
SECURITY DEFINER
SET search_path = ''
AS $$
BEGIN
  IF NEW.raw_user_meta_data ->> 'role' IN ('business', 'admin') THEN
    RETURN NEW;
  END IF;

  INSERT INTO public.profiles (id, first_name, last_name, location_city)
  VALUES (
    NEW.id,
    NULLIF(BTRIM(NEW.raw_user_meta_data ->> 'first_name'), ''),
    NULLIF(BTRIM(NEW.raw_user_meta_data ->> 'last_name'), ''),
    NULLIF(BTRIM(NEW.raw_user_meta_data ->> 'location_city'), '')
  )
  ON CONFLICT (id) DO NOTHING;
  RETURN NEW;
END;
$$;

REVOKE ALL ON FUNCTION public.handle_new_consumer() FROM PUBLIC;
REVOKE ALL ON FUNCTION public.handle_new_consumer() FROM anon, authenticated;

COMMIT;
