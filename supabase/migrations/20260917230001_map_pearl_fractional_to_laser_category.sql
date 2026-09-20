-- Keep the generated service category non-null for the newly supported laser.
create or replace function public.clinic_service_category_for_name(
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
      when 'Pearl Fractional' then 'Laser & Light'

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
