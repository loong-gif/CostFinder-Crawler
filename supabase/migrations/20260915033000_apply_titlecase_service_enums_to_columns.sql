-- Move canonical taxonomy onto clinic_services.service_name / service_category,
-- using title-case enum labels, then drop the generated *_enum columns.

alter table public.clinic_services
  drop column if exists service_name_enum,
  drop column if exists service_category_enum;

alter type public.clinic_service_name_enum rename value 'botox' to 'Botox';
alter type public.clinic_service_name_enum rename value 'dysport' to 'Dysport';
alter type public.clinic_service_name_enum rename value 'xeomin' to 'Xeomin';
alter type public.clinic_service_name_enum rename value 'jeuveau' to 'Jeuveau';
alter type public.clinic_service_name_enum rename value 'daxxify' to 'Daxxify';
alter type public.clinic_service_name_enum rename value 'letybo' to 'Letybo';
alter type public.clinic_service_name_enum rename value 'myobloc' to 'Myobloc';
alter type public.clinic_service_name_enum rename value 'neurotoxin' to 'Neurotoxin';
alter type public.clinic_service_name_enum rename value 'dermal_filler' to 'Dermal Filler';
alter type public.clinic_service_name_enum rename value 'juvederm' to 'Juvéderm';
alter type public.clinic_service_name_enum rename value 'restylane' to 'Restylane';
alter type public.clinic_service_name_enum rename value 'radiesse' to 'Radiesse';
alter type public.clinic_service_name_enum rename value 'belotero' to 'Belotero';
alter type public.clinic_service_name_enum rename value 'rha_filler' to 'Rha Filler';
alter type public.clinic_service_name_enum rename value 'skinvive' to 'Skinvive';
alter type public.clinic_service_name_enum rename value 'sculptra' to 'Sculptra';
alter type public.clinic_service_name_enum rename value 'filler_dissolving' to 'Filler Dissolving';
alter type public.clinic_service_name_enum rename value 'kybella' to 'Kybella';
alter type public.clinic_service_name_enum rename value 'morpheus8' to 'Morpheus8';
alter type public.clinic_service_name_enum rename value 'rf_microneedling' to 'Rf Microneedling';
alter type public.clinic_service_name_enum rename value 'skinpen_microneedling' to 'Skinpen Microneedling';
alter type public.clinic_service_name_enum rename value 'sylfirm_x' to 'Sylfirm X';
alter type public.clinic_service_name_enum rename value 'microchanneling' to 'Microchanneling';
alter type public.clinic_service_name_enum rename value 'microneedling' to 'Microneedling';
alter type public.clinic_service_name_enum rename value 'ez_gel' to 'Ez Gel';
alter type public.clinic_service_name_enum rename value 'prfm' to 'Prfm';
alter type public.clinic_service_name_enum rename value 'prf' to 'Prf';
alter type public.clinic_service_name_enum rename value 'prp' to 'Prp';
alter type public.clinic_service_name_enum rename value 'exosome' to 'Exosome';
alter type public.clinic_service_name_enum rename value 'pdrn' to 'Pdrn';
alter type public.clinic_service_name_enum rename value 'alloclae' to 'Alloclae';
alter type public.clinic_service_name_enum rename value 'hydrafacial' to 'Hydrafacial';
alter type public.clinic_service_name_enum rename value 'diamondglow' to 'Diamondglow';
alter type public.clinic_service_name_enum rename value 'dermaplaning' to 'Dermaplaning';
alter type public.clinic_service_name_enum rename value 'glo2_facial' to 'Glo2 Facial';
alter type public.clinic_service_name_enum rename value 'led_light_therapy' to 'Led Light Therapy';
alter type public.clinic_service_name_enum rename value 'facial' to 'Facial';
alter type public.clinic_service_name_enum rename value 'chemical_peel' to 'Chemical Peel';
alter type public.clinic_service_name_enum rename value 'ipl_photofacial' to 'Ipl Photofacial';
alter type public.clinic_service_name_enum rename value 'bbl_photofacial' to 'Bbl Photofacial';
alter type public.clinic_service_name_enum rename value 'co2_laser' to 'Co2 Laser';
alter type public.clinic_service_name_enum rename value 'coolpeel' to 'Coolpeel';
alter type public.clinic_service_name_enum rename value 'halo_laser' to 'Halo Laser';
alter type public.clinic_service_name_enum rename value 'moxi_laser' to 'Moxi Laser';
alter type public.clinic_service_name_enum rename value 'picoway' to 'Picoway';
alter type public.clinic_service_name_enum rename value 'vbeam' to 'Vbeam';
alter type public.clinic_service_name_enum rename value 'clearsilk' to 'Clearsilk';
alter type public.clinic_service_name_enum rename value 'laser_genesis' to 'Laser Genesis';
alter type public.clinic_service_name_enum rename value 'laser_hair_removal' to 'Laser Hair Removal';
alter type public.clinic_service_name_enum rename value 'tattoo_removal' to 'Tattoo Removal';
alter type public.clinic_service_name_enum rename value 'nail_fungus_treatment' to 'Nail Fungus Treatment';
alter type public.clinic_service_name_enum rename value 'laser_treatment' to 'Laser Treatment';
alter type public.clinic_service_name_enum rename value 'hifu' to 'Hifu';
alter type public.clinic_service_name_enum rename value 'xerf' to 'Xerf';
alter type public.clinic_service_name_enum rename value 'skin_tightening' to 'Skin Tightening';
alter type public.clinic_service_name_enum rename value 'coolsculpting' to 'Coolsculpting';
alter type public.clinic_service_name_enum rename value 'body_contouring' to 'Body Contouring';
alter type public.clinic_service_name_enum rename value 'thread_lift' to 'Thread Lift';
alter type public.clinic_service_name_enum rename value 'nad_therapy' to 'Nad Therapy';
alter type public.clinic_service_name_enum rename value 'iv_therapy' to 'Iv Therapy';
alter type public.clinic_service_name_enum rename value 'vitamin_injection' to 'Vitamin Injection';
alter type public.clinic_service_name_enum rename value 'weight_loss' to 'Weight Loss';
alter type public.clinic_service_name_enum rename value 'hormone_therapy' to 'Hormone Therapy';
alter type public.clinic_service_name_enum rename value 'peptide_therapy' to 'Peptide Therapy';
alter type public.clinic_service_name_enum rename value 'skincare_product' to 'Skincare Product';
alter type public.clinic_service_name_enum rename value 'surgery' to 'Surgery';
alter type public.clinic_service_name_enum rename value 'massage' to 'Massage';
alter type public.clinic_service_name_enum rename value 'skin_tag_removal' to 'Skin Tag Removal';
alter type public.clinic_service_name_enum rename value 'skin_analysis' to 'Skin Analysis';
alter type public.clinic_service_name_enum rename value 'non_service' to 'Non Service';
alter type public.clinic_service_name_enum rename value 'other' to 'Other';

alter type public.clinic_service_category_enum rename value 'neurotoxin' to 'Neurotoxin';
alter type public.clinic_service_category_enum rename value 'dermal_filler' to 'Dermal Filler';
alter type public.clinic_service_category_enum rename value 'biostimulator' to 'Biostimulator';
alter type public.clinic_service_category_enum rename value 'regenerative' to 'Regenerative';
alter type public.clinic_service_category_enum rename value 'injectable' to 'Injectable';
alter type public.clinic_service_category_enum rename value 'microneedling' to 'Microneedling';
alter type public.clinic_service_category_enum rename value 'facial' to 'Facial';
alter type public.clinic_service_category_enum rename value 'chemical_peel' to 'Chemical Peel';
alter type public.clinic_service_category_enum rename value 'laser' to 'Laser';
alter type public.clinic_service_category_enum rename value 'laser_hair_removal' to 'Laser Hair Removal';
alter type public.clinic_service_category_enum rename value 'tattoo_removal' to 'Tattoo Removal';
alter type public.clinic_service_category_enum rename value 'skin_tightening' to 'Skin Tightening';
alter type public.clinic_service_category_enum rename value 'body_contouring' to 'Body Contouring';
alter type public.clinic_service_category_enum rename value 'thread_lift' to 'Thread Lift';
alter type public.clinic_service_category_enum rename value 'wellness' to 'Wellness';
alter type public.clinic_service_category_enum rename value 'skincare_retail' to 'Skincare Retail';
alter type public.clinic_service_category_enum rename value 'surgery' to 'Surgery';
alter type public.clinic_service_category_enum rename value 'diagnostic' to 'Diagnostic';
alter type public.clinic_service_category_enum rename value 'other' to 'Other';
alter type public.clinic_service_category_enum rename value 'non_service' to 'Non Service';

create or replace function public.normalize_clinic_service_name(p_name text, p_category text default null)
returns public.clinic_service_name_enum
language sql
immutable
set search_path = pg_catalog, public
as $function$
  select (
    case
      when lower(coalesce(p_category, '')) in ('package', 'membership')
        or coalesce(p_name, '') ~* '(package|combo|trio|membership|beauty insider club|a-lister|glow getter|rising star|referral program|rewards program|event reservation|event treatment discount|medical spa credit|general treatment|new patient treatment|all services)'
        then 'Non Service'

      when coalesce(p_name, '') ~* '(^bbl$|bbl forever young)' then 'Bbl Photofacial'

      when coalesce(p_name, '') ~* 'botox' then 'Botox'
      when coalesce(p_name, '') ~* '^dysport$' then 'Dysport'
      when coalesce(p_name, '') ~* '^xeomin$' then 'Xeomin'
      when coalesce(p_name, '') ~* '^jeuveau$' then 'Jeuveau'
      when coalesce(p_name, '') ~* '^daxxify$' then 'Daxxify'
      when coalesce(p_name, '') ~* '^letybo$' then 'Letybo'
      when coalesce(p_name, '') ~* '^myobloc$' then 'Myobloc'
      when coalesce(p_name, '') ~* '(neurotoxin|wrinkle relaxer|quiktox|traptox|lip flip|gummy smile|chin tox|chin dimpling|jelly roll|under eye tox|brow lift)'
        or coalesce(p_name, '') ~* '^tox$' then 'Neurotoxin'

      when coalesce(p_name, '') ~* '^sculptra$' then 'Sculptra'
      when coalesce(p_name, '') ~* '^skinvive$' then 'Skinvive'
      when coalesce(p_name, '') ~* '^radiesse' or coalesce(p_name, '') ~* 'radiesse$' then 'Radiesse'
      when coalesce(p_name, '') ~* '^belotero$' then 'Belotero'
      when coalesce(p_name, '') ~* '^restylane' or coalesce(p_name, '') ~* '^kysse$' then 'Restylane'
      when coalesce(p_name, '') ~* '^juv' then 'Juvéderm'
      when coalesce(p_name, '') ~* '^rha filler$' then 'Rha Filler'
      when coalesce(p_name, '') ~* '(filler removal|hylenex)' then 'Filler Dissolving'
      when coalesce(p_name, '') ~* '(dermal filler|lip filler|undereye filler|nipple projection filler|full syringe filler|facial balancing)' then 'Dermal Filler'
      when coalesce(p_name, '') ~* '^kybella$' then 'Kybella'

      when coalesce(p_name, '') ~* '(morpheus8|morpheus rf)' then 'Morpheus8'
      when coalesce(p_name, '') ~* '(rf microneedling|exion rf)' then 'Rf Microneedling'
      when coalesce(p_name, '') ~* 'skinpen' then 'Skinpen Microneedling'
      when coalesce(p_name, '') ~* '^sylfirm x$' then 'Sylfirm X'
      when coalesce(p_name, '') ~* 'micro-channeling' then 'Microchanneling'
      when coalesce(p_name, '') ~* 'microneedling' then 'Microneedling'

      when coalesce(p_name, '') ~* '(ez gel|ezgel)' then 'Ez Gel'
      when coalesce(p_name, '') ~* '^prfm' then 'Prfm'
      when coalesce(p_name, '') ~* '^prf($| )' then 'Prf'
      when coalesce(p_name, '') ~* '^prp($| | injection| scalp| hair)' then 'Prp'
      when coalesce(p_name, '') ~* 'exosome' then 'Exosome'
      when coalesce(p_name, '') ~* '^pdrn($| )' then 'Pdrn'
      when coalesce(p_name, '') ~* '^alloclae$' then 'Alloclae'

      when coalesce(p_name, '') ~* 'hydrafacial|hydraglow facial' then 'Hydrafacial'
      when coalesce(p_name, '') ~* 'diamondglow' then 'Diamondglow'
      when coalesce(p_name, '') ~* 'dermaplane' then 'Dermaplaning'
      when coalesce(p_name, '') ~* 'glo2facial' then 'Glo2 Facial'
      when coalesce(p_name, '') ~* 'led light therapy' then 'Led Light Therapy'
      when coalesce(p_name, '') ~* 'facial|pearl face treatment|time rewind full face|neck treatment' then 'Facial'

      when coalesce(p_name, '') ~* '(chemical peel|stimulator peel|prx-c|prx plus|vi chemical peel)' then 'Chemical Peel'
      when coalesce(p_name, '') ~* '(ipl photofacial|photofacial)' then 'Ipl Photofacial'
      when coalesce(p_name, '') ~* '(^bbl$|bbl forever young)' then 'Bbl Photofacial'
      when coalesce(p_name, '') ~* 'co2 laser' then 'Co2 Laser'
      when coalesce(p_name, '') ~* 'cool ?peel' then 'Coolpeel'
      when coalesce(p_name, '') ~* 'halo' then 'Halo Laser'
      when coalesce(p_name, '') ~* 'moxi' then 'Moxi Laser'
      when coalesce(p_name, '') ~* 'picoway|^pico$' then 'Picoway'
      when coalesce(p_name, '') ~* '^vbeam$' then 'Vbeam'
      when coalesce(p_name, '') ~* 'clearsilk' then 'Clearsilk'
      when coalesce(p_name, '') ~* 'laser genesis' then 'Laser Genesis'
      when coalesce(p_name, '') ~* 'laser hair removal' then 'Laser Hair Removal'
      when coalesce(p_name, '') ~* 'tattoo removal' then 'Tattoo Removal'
      when coalesce(p_name, '') ~* 'nail fungus' then 'Nail Fungus Treatment'
      when coalesce(p_name, '') ~* 'nightlase' then 'Laser Treatment'
      when coalesce(p_name, '') ~* '(nano peel laser|laser facial|laser skin rejuvenation|laser services)' then 'Laser Treatment'

      when coalesce(p_name, '') ~* '^hifu$|hifu treatment' then 'Hifu'
      when coalesce(p_name, '') ~* '^xerf|xerf ' then 'Xerf'
      when coalesce(p_name, '') ~* 'skin tightening' then 'Skin Tightening'
      when coalesce(p_name, '') ~* 'coolsculpting' then 'Coolsculpting'
      when coalesce(p_name, '') ~* '(venus freeze|venus legacy|ems?lim|cryoskin|fat lipolysis|body contouring|trilift)' then 'Body Contouring'
      when coalesce(p_name, '') ~* '(thread lift|pdo threads)' then 'Thread Lift'

      when coalesce(p_name, '') ~* 'nad\\+' then 'Nad Therapy'
      when coalesce(p_name, '') ~* '(iv push|iv vitamin|iv nutrient)' then 'Iv Therapy'
      when coalesce(p_name, '') ~* '(b12|vitamin injection|skinny shot)' then 'Vitamin Injection'
      when coalesce(p_name, '') ~* '(weight loss|glp1)' then 'Weight Loss'
      when coalesce(p_name, '') ~* 'hormone pellet' then 'Hormone Therapy'
      when coalesce(p_name, '') ~* 'peptide boost' then 'Peptide Therapy'

      when coalesce(p_name, '') ~* '(skincare product|retail product|skincare shop|sunscreen|post procedure kit|epicutis premium set|ace ageless bundle|product sale)' then 'Skincare Product'
      when coalesce(p_name, '') ~* 'brachioplasty' then 'Surgery'
      when coalesce(p_name, '') ~* 'massage' then 'Massage'
      when coalesce(p_name, '') ~* 'skin tag removal' then 'Skin Tag Removal'
      when coalesce(p_name, '') ~* 'visia analysis' then 'Skin Analysis'

      when lower(coalesce(p_category, '')) = 'neurotoxin' then 'Neurotoxin'
      when lower(coalesce(p_category, '')) in ('filler', 'dermal filler') then 'Dermal Filler'
      when lower(coalesce(p_category, '')) = 'biostimulator' then 'Sculptra'
      when lower(coalesce(p_category, '')) in ('peel', 'chemical peel') then 'Chemical Peel'
      when lower(coalesce(p_category, '')) = 'facial' then 'Facial'
      when lower(coalesce(p_category, '')) in ('laser', 'picoway') then 'Laser Treatment'
      when lower(coalesce(p_category, '')) = 'laser hair removal' then 'Laser Hair Removal'
      when lower(coalesce(p_category, '')) = 'tattoo removal' then 'Tattoo Removal'
      when lower(coalesce(p_category, '')) = 'microneedling' then 'Microneedling'
      when lower(coalesce(p_category, '')) = 'skin tightening' then 'Skin Tightening'
      when lower(coalesce(p_category, '')) in ('retail', 'skincare', 'skincare bundle') then 'Skincare Product'
      when lower(coalesce(p_category, '')) = 'surgery' then 'Surgery'
      else 'Other'
    end
  )::public.clinic_service_name_enum;
$function$;

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
      when 'Rha Filler' then 'Dermal Filler'
      when 'Skinvive' then 'Dermal Filler'
      when 'Filler Dissolving' then 'Dermal Filler'

      when 'Sculptra' then 'Biostimulator'

      when 'Ez Gel' then 'Regenerative'
      when 'Prfm' then 'Regenerative'
      when 'Prf' then 'Regenerative'
      when 'Prp' then 'Regenerative'
      when 'Exosome' then 'Regenerative'
      when 'Pdrn' then 'Regenerative'

      when 'Kybella' then 'Injectable'
      when 'Alloclae' then 'Injectable'

      when 'Morpheus8' then 'Microneedling'
      when 'Rf Microneedling' then 'Microneedling'
      when 'Skinpen Microneedling' then 'Microneedling'
      when 'Sylfirm X' then 'Microneedling'
      when 'Microchanneling' then 'Microneedling'
      when 'Microneedling' then 'Microneedling'

      when 'Hydrafacial' then 'Facial'
      when 'Diamondglow' then 'Facial'
      when 'Dermaplaning' then 'Facial'
      when 'Glo2 Facial' then 'Facial'
      when 'Led Light Therapy' then 'Facial'
      when 'Facial' then 'Facial'

      when 'Chemical Peel' then 'Chemical Peel'

      when 'Ipl Photofacial' then 'Laser'
      when 'Bbl Photofacial' then 'Laser'
      when 'Co2 Laser' then 'Laser'
      when 'Coolpeel' then 'Laser'
      when 'Halo Laser' then 'Laser'
      when 'Moxi Laser' then 'Laser'
      when 'Picoway' then 'Laser'
      when 'Vbeam' then 'Laser'
      when 'Clearsilk' then 'Laser'
      when 'Laser Genesis' then 'Laser'
      when 'Laser Treatment' then 'Laser'

      when 'Laser Hair Removal' then 'Laser Hair Removal'
      when 'Tattoo Removal' then 'Tattoo Removal'

      when 'Hifu' then 'Skin Tightening'
      when 'Xerf' then 'Skin Tightening'
      when 'Skin Tightening' then 'Skin Tightening'

      when 'Coolsculpting' then 'Body Contouring'
      when 'Body Contouring' then 'Body Contouring'
      when 'Thread Lift' then 'Thread Lift'

      when 'Nad Therapy' then 'Wellness'
      when 'Iv Therapy' then 'Wellness'
      when 'Vitamin Injection' then 'Wellness'
      when 'Weight Loss' then 'Wellness'
      when 'Hormone Therapy' then 'Wellness'
      when 'Peptide Therapy' then 'Wellness'

      when 'Skincare Product' then 'Skincare Retail'
      when 'Surgery' then 'Surgery'
      when 'Skin Analysis' then 'Diagnostic'

      when 'Massage' then 'Other'
      when 'Skin Tag Removal' then 'Other'
      when 'Nail Fungus Treatment' then 'Other'
      when 'Other' then 'Other'
      when 'Non Service' then 'Non Service'
    end
  )::public.clinic_service_category_enum;
$function$;

alter table public.clinic_services
  alter column service_name type public.clinic_service_name_enum
  using public.normalize_clinic_service_name(service_name, service_category);

alter table public.clinic_services
  drop column service_category;

alter table public.clinic_services
  add column service_category public.clinic_service_category_enum
  generated always as (public.clinic_service_category_for_name(service_name)) stored not null;

comment on column public.clinic_services.service_name is
  'Canonical service name (clinic_service_name_enum), title-cased.';

comment on column public.clinic_services.service_category is
  'Canonical category derived from service_name via clinic_service_category_for_name.';
