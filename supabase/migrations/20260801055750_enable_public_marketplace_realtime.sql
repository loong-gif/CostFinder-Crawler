DO $$
DECLARE
  target_table text;
  marketplace_tables constant text[] := ARRAY[
    'promo_offer_master',
    'promo_offer_items',
    'clinic_services',
    'clinic_promotions',
    'master_business_info',
    'clinic_memberships'
  ];
BEGIN
  IF NOT EXISTS (
    SELECT 1
    FROM pg_publication
    WHERE pubname = 'supabase_realtime'
  ) THEN
    RAISE EXCEPTION 'Required publication supabase_realtime does not exist';
  END IF;

  FOREACH target_table IN ARRAY marketplace_tables LOOP
    IF to_regclass(format('public.%I', target_table)) IS NULL THEN
      RAISE EXCEPTION 'Required public table % does not exist', target_table;
    END IF;

    IF NOT EXISTS (
      SELECT 1
      FROM pg_class AS table_class
      JOIN pg_namespace AS table_namespace
        ON table_namespace.oid = table_class.relnamespace
      WHERE table_namespace.nspname = 'public'
        AND table_class.relname = target_table
        AND table_class.relrowsecurity
    ) THEN
      RAISE EXCEPTION 'RLS must remain enabled for public.%', target_table;
    END IF;

    IF NOT EXISTS (
      SELECT 1
      FROM pg_publication_tables
      WHERE pubname = 'supabase_realtime'
        AND schemaname = 'public'
        AND tablename = target_table
    ) THEN
      EXECUTE format(
        'ALTER PUBLICATION supabase_realtime ADD TABLE public.%I',
        target_table
      );
    END IF;
  END LOOP;
END
$$;
