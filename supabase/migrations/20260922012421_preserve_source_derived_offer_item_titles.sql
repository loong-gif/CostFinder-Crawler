-- Preserve source-derived deal titles while retaining service_id as a
-- classification link. This migration is intentionally generated locally only
-- during the 20260922 data repair; it has not been applied to Supabase.

begin;

create or replace function public.sync_promo_offer_item_name()
returns trigger
language plpgsql
set search_path = public
as $function$
declare
  v_service_name text;
begin
  -- A nonblank source title always wins. The catalogue is only a fallback for
  -- newly linked rows that have no display title at all.
  if nullif(btrim(new.item_name), '') is not null or new.service_id is null then
    return new;
  end if;

  select service_name::text
    into v_service_name
    from public.clinic_services
   where service_id = new.service_id;

  if found then
    new.item_name := v_service_name;
  end if;

  return new;
end;
$function$;

create or replace function public.sync_promo_offer_item_name_from_service()
returns trigger
language plpgsql
set search_path = public
as $function$
begin
  -- service_name is classification vocabulary, not source copy. Existing
  -- promo_offer_items retain their source-derived display titles on renames.
  return null;
end;
$function$;

comment on column public.promo_offer_items.item_name is
  'Source-derived deal/item display title. A linked clinic service name is used only when item_name is missing.';

commit;
