create type public.clinic_service_category_enum as enum (
  'neurotoxin', 'dermal_filler', 'biostimulator', 'regenerative', 'injectable',
  'microneedling', 'facial', 'chemical_peel', 'laser', 'laser_hair_removal',
  'tattoo_removal', 'skin_tightening', 'body_contouring', 'thread_lift', 'wellness',
  'skincare_retail', 'surgery', 'diagnostic', 'other', 'non_service'
);

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
      when 'botox' then 'neurotoxin'
      when 'dysport' then 'neurotoxin'
      when 'xeomin' then 'neurotoxin'
      when 'jeuveau' then 'neurotoxin'
      when 'daxxify' then 'neurotoxin'
      when 'letybo' then 'neurotoxin'
      when 'myobloc' then 'neurotoxin'
      when 'neurotoxin' then 'neurotoxin'

      when 'dermal_filler' then 'dermal_filler'
      when 'juvederm' then 'dermal_filler'
      when 'restylane' then 'dermal_filler'
      when 'radiesse' then 'dermal_filler'
      when 'belotero' then 'dermal_filler'
      when 'rha_filler' then 'dermal_filler'
      when 'skinvive' then 'dermal_filler'
      when 'filler_dissolving' then 'dermal_filler'

      when 'sculptra' then 'biostimulator'

      when 'ez_gel' then 'regenerative'
      when 'prfm' then 'regenerative'
      when 'prf' then 'regenerative'
      when 'prp' then 'regenerative'
      when 'exosome' then 'regenerative'
      when 'pdrn' then 'regenerative'

      when 'kybella' then 'injectable'
      when 'alloclae' then 'injectable'

      when 'morpheus8' then 'microneedling'
      when 'rf_microneedling' then 'microneedling'
      when 'skinpen_microneedling' then 'microneedling'
      when 'sylfirm_x' then 'microneedling'
      when 'microchanneling' then 'microneedling'
      when 'microneedling' then 'microneedling'

      when 'hydrafacial' then 'facial'
      when 'diamondglow' then 'facial'
      when 'dermaplaning' then 'facial'
      when 'glo2_facial' then 'facial'
      when 'led_light_therapy' then 'facial'
      when 'facial' then 'facial'

      when 'chemical_peel' then 'chemical_peel'

      when 'ipl_photofacial' then 'laser'
      when 'bbl_photofacial' then 'laser'
      when 'co2_laser' then 'laser'
      when 'coolpeel' then 'laser'
      when 'halo_laser' then 'laser'
      when 'moxi_laser' then 'laser'
      when 'picoway' then 'laser'
      when 'vbeam' then 'laser'
      when 'clearsilk' then 'laser'
      when 'laser_genesis' then 'laser'
      when 'laser_treatment' then 'laser'

      when 'laser_hair_removal' then 'laser_hair_removal'
      when 'tattoo_removal' then 'tattoo_removal'

      when 'hifu' then 'skin_tightening'
      when 'xerf' then 'skin_tightening'
      when 'skin_tightening' then 'skin_tightening'

      when 'coolsculpting' then 'body_contouring'
      when 'body_contouring' then 'body_contouring'
      when 'thread_lift' then 'thread_lift'

      when 'nad_therapy' then 'wellness'
      when 'iv_therapy' then 'wellness'
      when 'vitamin_injection' then 'wellness'
      when 'weight_loss' then 'wellness'
      when 'hormone_therapy' then 'wellness'
      when 'peptide_therapy' then 'wellness'

      when 'skincare_product' then 'skincare_retail'
      when 'surgery' then 'surgery'
      when 'skin_analysis' then 'diagnostic'

      when 'massage' then 'other'
      when 'skin_tag_removal' then 'other'
      when 'nail_fungus_treatment' then 'other'
      when 'other' then 'other'
      when 'non_service' then 'non_service'
    end
  )::public.clinic_service_category_enum;
$function$;

alter table public.clinic_services
  add column service_category_enum public.clinic_service_category_enum
    generated always as (
      public.clinic_service_category_for_name(
        public.normalize_clinic_service_name(service_name, service_category)
      )
    ) stored not null;
