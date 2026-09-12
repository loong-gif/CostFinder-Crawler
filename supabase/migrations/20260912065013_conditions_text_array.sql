-- Keep the three-state meaning of conditions while simplifying the payload:
-- NULL = not processed/review, '{}' = processed with no extra restrictions,
-- and a non-empty text[] contains only requirement strings.
alter table public.promo_offer_master rename column conditions to conditions_jsonb;
alter table public.promo_offer_master add column conditions text[];

update public.promo_offer_master as offer
set conditions = case
  when offer.conditions_jsonb is null then null::text[]
  else coalesce(
    (
      select array_agg(extracted.requirement order by extracted.position)
      from (
        select
          case
            when left(item.value::text, 1) = '{'
              then ((item.value::text)::jsonb ->> 'requirement')
            when left(item.value::text, 1) = '"'
              then ((item.value::text)::jsonb #>> '{}')
            else item.value::text
          end as requirement,
          item.position
        from unnest(offer.conditions_jsonb) with ordinality as item(value, position)
      ) as extracted
      where nullif(btrim(extracted.requirement), '') is not null
    ),
    '{}'::text[]
  )
end;

alter table public.promo_offer_master drop column conditions_jsonb;
