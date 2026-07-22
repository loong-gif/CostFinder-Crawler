BEGIN;

CREATE TABLE IF NOT EXISTS public.profiles (
  id uuid PRIMARY KEY REFERENCES auth.users(id) ON DELETE CASCADE,
  first_name text,
  last_name text,
  avatar_url text,
  phone text,
  phone_verified_at timestamptz,
  verification_status text NOT NULL DEFAULT 'unverified'
    CHECK (verification_status IN ('unverified', 'email_verified', 'phone_verified', 'fully_verified')),
  location_city text,
  location_state text,
  alerts_email boolean NOT NULL DEFAULT true,
  alerts_sms boolean NOT NULL DEFAULT false,
  favorite_categories text[] NOT NULL DEFAULT '{}'::text[],
  status text NOT NULL DEFAULT 'active'
    CHECK (status IN ('active', 'suspended')),
  created_at timestamptz NOT NULL DEFAULT now(),
  updated_at timestamptz NOT NULL DEFAULT now(),
  last_login_at timestamptz
);

ALTER TABLE public.profiles ENABLE ROW LEVEL SECURITY;
GRANT SELECT, UPDATE ON public.profiles TO authenticated;

DROP POLICY IF EXISTS "consumer_select_own_profile" ON public.profiles;
CREATE POLICY "consumer_select_own_profile"
  ON public.profiles FOR SELECT TO authenticated
  USING ((SELECT auth.uid()) = id);

DROP POLICY IF EXISTS "consumer_update_own_profile" ON public.profiles;
CREATE POLICY "consumer_update_own_profile"
  ON public.profiles FOR UPDATE TO authenticated
  USING ((SELECT auth.uid()) = id)
  WITH CHECK ((SELECT auth.uid()) = id);

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

  INSERT INTO public.profiles (id, first_name, last_name)
  VALUES (
    NEW.id,
    NULLIF(BTRIM(NEW.raw_user_meta_data ->> 'first_name'), ''),
    NULLIF(BTRIM(NEW.raw_user_meta_data ->> 'last_name'), '')
  )
  ON CONFLICT (id) DO NOTHING;
  RETURN NEW;
END;
$$;

REVOKE ALL ON FUNCTION public.handle_new_consumer() FROM PUBLIC;
REVOKE ALL ON FUNCTION public.handle_new_consumer() FROM anon, authenticated;

DROP TRIGGER IF EXISTS on_auth_consumer_created ON auth.users;
CREATE TRIGGER on_auth_consumer_created
  AFTER INSERT ON auth.users
  FOR EACH ROW EXECUTE FUNCTION public.handle_new_consumer();

DO $$
BEGIN
  IF NOT EXISTS (
    SELECT 1 FROM pg_class
    WHERE oid = 'public.profiles'::regclass AND relrowsecurity
  ) THEN
    RAISE EXCEPTION 'profiles RLS must be enabled';
  END IF;
  IF NOT EXISTS (
    SELECT 1 FROM pg_trigger
    WHERE tgname = 'on_auth_consumer_created' AND NOT tgisinternal
  ) THEN
    RAISE EXCEPTION 'consumer profile trigger is missing';
  END IF;
END;
$$;

COMMIT;
