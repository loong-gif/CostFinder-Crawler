CREATE OR REPLACE FUNCTION public.clinic_service_category_for_name(
  p_service public.clinic_service_name_enum
)
RETURNS public.clinic_service_category_enum
LANGUAGE sql
IMMUTABLE
SET search_path TO pg_catalog, public
AS $function$
  SELECT (
    CASE p_service
      WHEN 'Botox' THEN 'Neurotoxin'
      WHEN 'Dysport' THEN 'Neurotoxin'
      WHEN 'Xeomin' THEN 'Neurotoxin'
      WHEN 'Jeuveau' THEN 'Neurotoxin'
      WHEN 'Daxxify' THEN 'Neurotoxin'
      WHEN 'Letybo' THEN 'Neurotoxin'
      WHEN 'Myobloc' THEN 'Neurotoxin'
      WHEN 'Neurotoxin' THEN 'Neurotoxin'

      WHEN 'Dermal Filler' THEN 'Dermal Filler'
      WHEN 'Juvéderm' THEN 'Dermal Filler'
      WHEN 'Restylane' THEN 'Dermal Filler'
      WHEN 'Radiesse' THEN 'Dermal Filler'
      WHEN 'Belotero' THEN 'Dermal Filler'
      WHEN 'RHA Filler' THEN 'Dermal Filler'
      WHEN 'Skinvive' THEN 'Dermal Filler'
      WHEN 'Filler Dissolving' THEN 'Dermal Filler'
      WHEN 'Sculptra' THEN 'Dermal Filler'
      WHEN 'Thread Lift' THEN 'Dermal Filler'
      WHEN 'Versa' THEN 'Dermal Filler'
      WHEN 'Evolysse' THEN 'Dermal Filler'

      WHEN 'Morpheus8' THEN 'Microneedling'
      WHEN 'RF Microneedling' THEN 'Microneedling'
      WHEN 'SkinPen Microneedling' THEN 'Microneedling'
      WHEN 'Sylfirm X' THEN 'Microneedling'
      WHEN 'Microchanneling' THEN 'Microneedling'
      WHEN 'Microneedling' THEN 'Microneedling'

      WHEN 'Hydrafacial' THEN 'Facial'
      WHEN 'DiamondGlow' THEN 'Facial'
      WHEN 'Dermaplaning' THEN 'Facial'
      WHEN 'Glo2 Facial' THEN 'Facial'
      WHEN 'Facial' THEN 'Facial'
      WHEN 'Chemical Peel' THEN 'Facial'
      WHEN 'Customized Facial' THEN 'Facial'
      WHEN 'Lunchtime Peel' THEN 'Facial'
      WHEN 'PRX Skin Perfecting' THEN 'Facial'
      WHEN 'Perfect Derma Peel' THEN 'Facial'
      WHEN 'VI Peel' THEN 'Facial'
      WHEN 'Microdermabrasion' THEN 'Facial'

      WHEN 'IPL Photofacial' THEN 'Laser & Light'
      WHEN 'BBL Photofacial' THEN 'Laser & Light'
      WHEN 'LED Light Therapy' THEN 'Laser & Light'
      WHEN 'CO2 Laser' THEN 'Laser & Light'
      WHEN 'CoolPeel' THEN 'Laser & Light'
      WHEN 'Halo Laser' THEN 'Laser & Light'
      WHEN 'Moxi Laser' THEN 'Laser & Light'
      WHEN 'Picoway' THEN 'Laser & Light'
      WHEN 'Vbeam' THEN 'Laser & Light'
      WHEN 'ClearSilk' THEN 'Laser & Light'
      WHEN 'Laser Genesis' THEN 'Laser & Light'
      WHEN 'Laser Treatment' THEN 'Laser & Light'
      WHEN 'Pearl Fractional' THEN 'Laser & Light'
      WHEN 'Tattoo Removal' THEN 'Laser & Light'
      WHEN 'Fraxel' THEN 'Laser & Light'
      WHEN 'Clear + Brilliant' THEN 'Laser & Light'

      WHEN 'Skin Tightening' THEN 'Skin Tightening'
      WHEN 'Sofwave' THEN 'Skin Tightening'
      WHEN 'Tixel' THEN 'Skin Tightening'
      WHEN 'Ultherapy' THEN 'Skin Tightening'
      WHEN 'Thermage' THEN 'Skin Tightening'
      WHEN 'HIFU' THEN 'Skin Tightening'
      WHEN 'Forma' THEN 'Skin Tightening'
      WHEN 'Emface' THEN 'Skin Tightening'

      WHEN 'Laser Hair Removal' THEN 'Laser Hair Removal'

      WHEN 'NAD Therapy' THEN 'Wellness'
      WHEN 'IV Therapy' THEN 'Wellness'
      WHEN 'Vitamin Injection' THEN 'Wellness'
      WHEN 'Weight Loss' THEN 'Wellness'
      WHEN 'Hormone Therapy' THEN 'Wellness'
      WHEN 'Peptide Therapy' THEN 'Wellness'
      WHEN 'Wellness' THEN 'Wellness'
      WHEN 'Massage' THEN 'Wellness'
      WHEN 'Hair Restoration' THEN 'Wellness'

      WHEN 'Regenerative Injectable' THEN 'Regenerative Injectable'
      WHEN 'PRP' THEN 'Regenerative Injectable'
      WHEN 'PRF' THEN 'Regenerative Injectable'
      WHEN 'Exosome Therapy' THEN 'Regenerative Injectable'
      WHEN 'EZ Gel' THEN 'Regenerative Injectable'
      WHEN 'PDRN' THEN 'Regenerative Injectable'
      WHEN 'PRFM' THEN 'Regenerative Injectable'

      WHEN 'Body Contouring' THEN 'Body Contouring'
      WHEN 'CoolSculpting' THEN 'Body Contouring'
      WHEN 'Kybella' THEN 'Body Contouring'
      WHEN 'Lipo Dissolve' THEN 'Body Contouring'
      WHEN 'Emsculpt' THEN 'Body Contouring'
    END
  )::public.clinic_service_category_enum;
$function$;
