begin;

create or replace function public.sync_promo_offer_item_name()
returns trigger
language plpgsql
set search_path = public
as $function$
declare
  v_service_name text;
begin
  if new.service_id is null or new.item_name is not null then
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
  update public.promo_offer_items
     set item_name = new.service_name::text
   where service_id = new.service_id
     and item_name is null;

  return null;
end;
$function$;

comment on column public.promo_offer_items.item_name is
  'Source-facing deal/item title. Preserve every non-NULL extracted value. service_id links the item to normalized clinic_services; clinic_services.service_name is a fallback only when item_name is NULL.';

commit;
