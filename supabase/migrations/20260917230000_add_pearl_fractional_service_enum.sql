-- Pearl Fractional is explicitly named by the Skin Goals membership source.
-- Keep it as a canonical service so the membership can be fully represented.
alter type public.clinic_service_name_enum
  add value if not exists 'Pearl Fractional';
