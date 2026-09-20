-- Retain only the approved service taxonomy. The direct enum-backed
-- service_name column already has raw evidence preserved in scrape storage.

begin;

create type public.clinic_service_name_enum_next as enum (
  'Botox', 'Dysport', 'Xeomin', 'Jeuveau', 'Daxxify', 'Letybo', 'Myobloc', 'Neurotoxin',
  'Dermal Filler', 'Juvéderm', 'Restylane', 'Radiesse', 'Belotero', 'RHA Filler', 'Skinvive', 'Filler Dissolving',
  'Morpheus8', 'RF Microneedling', 'SkinPen Microneedling', 'Sylfirm X', 'Microchanneling', 'Microneedling',
  'Hydrafacial', 'DiamondGlow', 'Dermaplaning', 'Glo2 Facial', 'Facial',
  'IPL Photofacial', 'BBL Photofacial', 'LED Light Therapy', 'CO2 Laser', 'CoolPeel', 'Halo Laser', 'Moxi Laser', 'Picoway', 'Vbeam', 'ClearSilk', 'Laser Genesis', 'Laser Treatment',
  'Laser Hair Removal',
  'NAD Therapy', 'IV Therapy', 'Vitamin Injection', 'Weight Loss', 'Hormone Therapy', 'Peptide Therapy'
);

create type public.clinic_service_category_enum_next as enum (
  'Neurotoxin',
  'Dermal Filler',
  'Microneedling',
  'Facial',
  'Laser & Light',
  'Laser Hair Removal',
  'Wellness'
);

-- This deletion is intentional: the user-approved taxonomy excludes these
-- service rows. promo_offer_items.service_id uses ON DELETE SET NULL.
delete from public.clinic_services
where service_name::text not in (
  'Botox', 'Dysport', 'Xeomin', 'Jeuveau', 'Daxxify', 'Letybo', 'Myobloc', 'Neurotoxin',
  'Dermal Filler', 'Juvéderm', 'Restylane', 'Radiesse', 'Belotero', 'RHA Filler', 'Skinvive', 'Filler Dissolving',
  'Morpheus8', 'RF Microneedling', 'SkinPen Microneedling', 'Sylfirm X', 'Microchanneling', 'Microneedling',
  'Hydrafacial', 'DiamondGlow', 'Dermaplaning', 'Glo2 Facial', 'Facial',
  'IPL Photofacial', 'BBL Photofacial', 'LED Light Therapy', 'CO2 Laser', 'CoolPeel', 'Halo Laser', 'Moxi Laser', 'Picoway', 'Vbeam', 'ClearSilk', 'Laser Genesis', 'Laser Treatment',
  'Laser Hair Removal',
  'NAD Therapy', 'IV Therapy', 'Vitamin Injection', 'Weight Loss', 'Hormone Therapy', 'Peptide Therapy'
);

alter table public.clinic_services
  drop column service_category;

-- PostgreSQL cannot change a trigger-targeted column's enum type in place.
-- The trigger body remains valid and is recreated after the type replacement.
drop trigger trg_sync_promo_offer_item_name_from_service on public.clinic_services;

drop function public.clinic_service_category_for_name(public.clinic_service_name_enum);
drop function public.normalize_clinic_service_name(text, text);

alter table public.clinic_services
  alter column service_name type public.clinic_service_name_enum_next
  using service_name::text::public.clinic_service_name_enum_next;

drop type public.clinic_service_name_enum;
drop type public.clinic_service_category_enum;

alter type public.clinic_service_name_enum_next rename to clinic_service_name_enum;
alter type public.clinic_service_category_enum_next rename to clinic_service_category_enum;

create function public.clinic_service_category_for_name(
  p_service public.clinic_service_name_enum
)
returns public.clinic_service_category_enum
language sql
immutable
set search_path = pg_catalog, public
as $function$
  select (
    case p_service
      when 'Botox' then 'Neurotoxin'
      when 'Dysport' then 'Neurotoxin'
      when 'Xeomin' then 'Neurotoxin'
      when 'Jeuveau' then 'Neurotoxin'
      when 'Daxxify' then 'Neurotoxin'
      when 'Letybo' then 'Neurotoxin'
      when 'Myobloc' then 'Neurotoxin'
      when 'Neurotoxin' then 'Neurotoxin'

      when 'Dermal Filler' then 'Dermal Filler'
      when 'Juvéderm' then 'Dermal Filler'
      when 'Restylane' then 'Dermal Filler'
      when 'Radiesse' then 'Dermal Filler'
      when 'Belotero' then 'Dermal Filler'
      when 'RHA Filler' then 'Dermal Filler'
      when 'Skinvive' then 'Dermal Filler'
      when 'Filler Dissolving' then 'Dermal Filler'

      when 'Morpheus8' then 'Microneedling'
      when 'RF Microneedling' then 'Microneedling'
      when 'SkinPen Microneedling' then 'Microneedling'
      when 'Sylfirm X' then 'Microneedling'
      when 'Microchanneling' then 'Microneedling'
      when 'Microneedling' then 'Microneedling'

      when 'Hydrafacial' then 'Facial'
      when 'DiamondGlow' then 'Facial'
      when 'Dermaplaning' then 'Facial'
      when 'Glo2 Facial' then 'Facial'
      when 'Facial' then 'Facial'

      when 'IPL Photofacial' then 'Laser & Light'
      when 'BBL Photofacial' then 'Laser & Light'
      when 'LED Light Therapy' then 'Laser & Light'
      when 'CO2 Laser' then 'Laser & Light'
      when 'CoolPeel' then 'Laser & Light'
      when 'Halo Laser' then 'Laser & Light'
      when 'Moxi Laser' then 'Laser & Light'
      when 'Picoway' then 'Laser & Light'
      when 'Vbeam' then 'Laser & Light'
      when 'ClearSilk' then 'Laser & Light'
      when 'Laser Genesis' then 'Laser & Light'
      when 'Laser Treatment' then 'Laser & Light'

      when 'Laser Hair Removal' then 'Laser Hair Removal'

      when 'NAD Therapy' then 'Wellness'
      when 'IV Therapy' then 'Wellness'
      when 'Vitamin Injection' then 'Wellness'
      when 'Weight Loss' then 'Wellness'
      when 'Hormone Therapy' then 'Wellness'
      when 'Peptide Therapy' then 'Wellness'
    end
  )::public.clinic_service_category_enum;
$function$;

alter table public.clinic_services
  add column service_category public.clinic_service_category_enum
  generated always as (public.clinic_service_category_for_name(service_name)) stored not null;

create trigger trg_sync_promo_offer_item_name_from_service
after update of service_name on public.clinic_services
for each row execute function public.sync_promo_offer_item_name_from_service();

comment on column public.clinic_services.service_name is
  'Canonical service name constrained to the approved clinic_service_name_enum.';

comment on column public.clinic_services.service_category is
  'Canonical category generated deterministically from service_name.';

commit;
