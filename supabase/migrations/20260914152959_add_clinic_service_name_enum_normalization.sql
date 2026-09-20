create type public.clinic_service_name_enum as enum (
  'botox', 'dysport', 'xeomin', 'jeuveau', 'daxxify', 'letybo', 'myobloc', 'neurotoxin',
  'dermal_filler', 'juvederm', 'restylane', 'radiesse', 'belotero', 'rha_filler', 'skinvive',
  'sculptra', 'filler_dissolving', 'kybella', 'morpheus8', 'rf_microneedling',
  'skinpen_microneedling', 'sylfirm_x', 'microchanneling', 'microneedling', 'ez_gel', 'prfm',
  'prf', 'prp', 'exosome', 'pdrn', 'alloclae', 'hydrafacial', 'diamondglow', 'dermaplaning',
  'glo2_facial', 'led_light_therapy', 'facial', 'chemical_peel', 'ipl_photofacial',
  'bbl_photofacial', 'co2_laser', 'coolpeel', 'halo_laser', 'moxi_laser', 'picoway', 'vbeam',
  'clearsilk', 'laser_genesis', 'laser_hair_removal', 'tattoo_removal', 'nail_fungus_treatment',
  'laser_treatment', 'hifu', 'xerf', 'skin_tightening', 'coolsculpting', 'body_contouring',
  'thread_lift', 'nad_therapy', 'iv_therapy', 'vitamin_injection', 'weight_loss',
  'hormone_therapy', 'peptide_therapy', 'skincare_product', 'surgery', 'massage',
  'skin_tag_removal', 'skin_analysis', 'non_service', 'other'
);

create function public.normalize_clinic_service_name(p_name text, p_category text default null)
returns public.clinic_service_name_enum
language sql
immutable
set search_path = pg_catalog, public
as $function$
  select (
    case
      when lower(coalesce(p_category, '')) in ('package', 'membership')
        or coalesce(p_name, '') ~* '(package|combo|trio|membership|beauty insider club|a-lister|glow getter|rising star|referral program|rewards program|event reservation|event treatment discount|medical spa credit|general treatment|new patient treatment|all services)'
        then 'non_service'

      when coalesce(p_name, '') ~* '(^bbl$|bbl forever young)' then 'bbl_photofacial'
      when coalesce(p_name, '') ~* 'botox' then 'botox'
      when coalesce(p_name, '') ~* '^dysport$' then 'dysport'
      when coalesce(p_name, '') ~* '^xeomin$' then 'xeomin'
      when coalesce(p_name, '') ~* '^jeuveau$' then 'jeuveau'
      when coalesce(p_name, '') ~* '^daxxify$' then 'daxxify'
      when coalesce(p_name, '') ~* '^letybo$' then 'letybo'
      when coalesce(p_name, '') ~* '^myobloc$' then 'myobloc'
      when coalesce(p_name, '') ~* '(neurotoxin|wrinkle relaxer|quiktox|traptox|lip flip|gummy smile|chin tox|chin dimpling|jelly roll|under eye tox|brow lift)'
        or coalesce(p_name, '') ~* '^tox$' then 'neurotoxin'

      when coalesce(p_name, '') ~* '^sculptra$' then 'sculptra'
      when coalesce(p_name, '') ~* '^skinvive$' then 'skinvive'
      when coalesce(p_name, '') ~* '^radiesse' or coalesce(p_name, '') ~* 'radiesse$' then 'radiesse'
      when coalesce(p_name, '') ~* '^belotero$' then 'belotero'
      when coalesce(p_name, '') ~* '^restylane' or coalesce(p_name, '') ~* '^kysse$' then 'restylane'
      when coalesce(p_name, '') ~* '^juv' then 'juvederm'
      when coalesce(p_name, '') ~* '^rha filler$' then 'rha_filler'
      when coalesce(p_name, '') ~* '(filler removal|hylenex)' then 'filler_dissolving'
      when coalesce(p_name, '') ~* '(dermal filler|lip filler|undereye filler|nipple projection filler|full syringe filler|facial balancing)' then 'dermal_filler'
      when coalesce(p_name, '') ~* '^kybella$' then 'kybella'

      when coalesce(p_name, '') ~* '(morpheus8|morpheus rf)' then 'morpheus8'
      when coalesce(p_name, '') ~* '(rf microneedling|exion rf)' then 'rf_microneedling'
      when coalesce(p_name, '') ~* 'skinpen' then 'skinpen_microneedling'
      when coalesce(p_name, '') ~* '^sylfirm x$' then 'sylfirm_x'
      when coalesce(p_name, '') ~* 'micro-channeling' then 'microchanneling'
      when coalesce(p_name, '') ~* 'microneedling' then 'microneedling'

      when coalesce(p_name, '') ~* '(ez gel|ezgel)' then 'ez_gel'
      when coalesce(p_name, '') ~* '^prfm' then 'prfm'
      when coalesce(p_name, '') ~* '^prf($| )' then 'prf'
      when coalesce(p_name, '') ~* '^prp($| | injection| scalp| hair)' then 'prp'
      when coalesce(p_name, '') ~* 'exosome' then 'exosome'
      when coalesce(p_name, '') ~* '^pdrn($| )' then 'pdrn'
      when coalesce(p_name, '') ~* '^alloclae$' then 'alloclae'

      when coalesce(p_name, '') ~* 'hydrafacial|hydraglow facial' then 'hydrafacial'
      when coalesce(p_name, '') ~* 'diamondglow' then 'diamondglow'
      when coalesce(p_name, '') ~* 'dermaplane' then 'dermaplaning'
      when coalesce(p_name, '') ~* 'glo2facial' then 'glo2_facial'
      when coalesce(p_name, '') ~* 'led light therapy' then 'led_light_therapy'
      when coalesce(p_name, '') ~* 'facial|pearl face treatment|time rewind full face|neck treatment' then 'facial'

      when coalesce(p_name, '') ~* '(chemical peel|stimulator peel|prx-c|prx plus|vi chemical peel)' then 'chemical_peel'
      when coalesce(p_name, '') ~* '(ipl photofacial|photofacial)' then 'ipl_photofacial'
      when coalesce(p_name, '') ~* 'co2 laser' then 'co2_laser'
      when coalesce(p_name, '') ~* 'cool ?peel' then 'coolpeel'
      when coalesce(p_name, '') ~* 'halo' then 'halo_laser'
      when coalesce(p_name, '') ~* 'moxi' then 'moxi_laser'
      when coalesce(p_name, '') ~* 'picoway|^pico$' then 'picoway'
      when coalesce(p_name, '') ~* '^vbeam$' then 'vbeam'
      when coalesce(p_name, '') ~* 'clearsilk' then 'clearsilk'
      when coalesce(p_name, '') ~* 'laser genesis' then 'laser_genesis'
      when coalesce(p_name, '') ~* 'laser hair removal' then 'laser_hair_removal'
      when coalesce(p_name, '') ~* 'tattoo removal' then 'tattoo_removal'
      when coalesce(p_name, '') ~* 'nail fungus' then 'nail_fungus_treatment'
      when coalesce(p_name, '') ~* 'nightlase' then 'laser_treatment'
      when coalesce(p_name, '') ~* '(nano peel laser|laser facial|laser skin rejuvenation|laser services)' then 'laser_treatment'

      when coalesce(p_name, '') ~* '^hifu$|hifu treatment' then 'hifu'
      when coalesce(p_name, '') ~* '^xerf|xerf ' then 'xerf'
      when coalesce(p_name, '') ~* 'skin tightening' then 'skin_tightening'
      when coalesce(p_name, '') ~* 'coolsculpting' then 'coolsculpting'
      when coalesce(p_name, '') ~* '(venus freeze|venus legacy|ems?lim|cryoskin|fat lipolysis|body contouring|trilift)' then 'body_contouring'
      when coalesce(p_name, '') ~* '(thread lift|pdo threads)' then 'thread_lift'

      when coalesce(p_name, '') ~* 'nad\\+' then 'nad_therapy'
      when coalesce(p_name, '') ~* '(iv push|iv vitamin|iv nutrient)' then 'iv_therapy'
      when coalesce(p_name, '') ~* '(b12|vitamin injection|skinny shot)' then 'vitamin_injection'
      when coalesce(p_name, '') ~* '(weight loss|glp1)' then 'weight_loss'
      when coalesce(p_name, '') ~* 'hormone pellet' then 'hormone_therapy'
      when coalesce(p_name, '') ~* 'peptide boost' then 'peptide_therapy'

      when coalesce(p_name, '') ~* '(skincare product|retail product|skincare shop|sunscreen|post procedure kit|epicutis premium set|ace ageless bundle|product sale)' then 'skincare_product'
      when coalesce(p_name, '') ~* 'brachioplasty' then 'surgery'
      when coalesce(p_name, '') ~* 'massage' then 'massage'
      when coalesce(p_name, '') ~* 'skin tag removal' then 'skin_tag_removal'
      when coalesce(p_name, '') ~* 'visia analysis' then 'skin_analysis'

      when lower(coalesce(p_category, '')) = 'neurotoxin' then 'neurotoxin'
      when lower(coalesce(p_category, '')) in ('filler', 'dermal filler') then 'dermal_filler'
      when lower(coalesce(p_category, '')) = 'biostimulator' then 'sculptra'
      when lower(coalesce(p_category, '')) in ('peel', 'chemical peel') then 'chemical_peel'
      when lower(coalesce(p_category, '')) = 'facial' then 'facial'
      when lower(coalesce(p_category, '')) in ('laser', 'picoway') then 'laser_treatment'
      when lower(coalesce(p_category, '')) = 'laser hair removal' then 'laser_hair_removal'
      when lower(coalesce(p_category, '')) = 'tattoo removal' then 'tattoo_removal'
      when lower(coalesce(p_category, '')) = 'microneedling' then 'microneedling'
      when lower(coalesce(p_category, '')) = 'skin tightening' then 'skin_tightening'
      when lower(coalesce(p_category, '')) in ('retail', 'skincare', 'skincare bundle') then 'skincare_product'
      when lower(coalesce(p_category, '')) = 'surgery' then 'surgery'
      else 'other'
    end
  )::public.clinic_service_name_enum;
$function$;

alter table public.clinic_services
  add column service_name_enum public.clinic_service_name_enum
    generated always as (public.normalize_clinic_service_name(service_name, service_category)) stored;
