-- Keep the Offer-to-terms trigger aligned with the current table schema.
-- promo_offer_terms no longer has is_non_member_required.
create or replace function public.sync_offer_terms_from_legacy_fields()
returns trigger
language plpgsql
set search_path = ''
as $function$
declare
  v_raw_terms text[];
  v_returning_required boolean;
begin
  v_raw_terms := coalesce(new.conditions, '{}'::text[]);
  v_returning_required := exists (
    select 1
    from unnest(v_raw_terms) c
    where lower(c) like '%returning customer required%'
  );

  insert into public.promo_offer_terms (
    offer_id,
    is_new_customer_required,
    is_returning_customer_required,
    is_membership_required,
    membership_plan_id,
    raw_terms
  ) values (
    new.id,
    case
      when new.is_new_customer_required is true then true
      when v_returning_required then false
      else new.is_new_customer_required
    end,
    case
      when new.is_new_customer_required is true then false
      when v_returning_required then true
      else null
    end,
    case
      when new.membership_plan_id is not null then true
      else new.is_membership_required
    end,
    new.membership_plan_id,
    v_raw_terms
  )
  on conflict (offer_id) do update set
    is_new_customer_required = excluded.is_new_customer_required,
    is_returning_customer_required = excluded.is_returning_customer_required,
    is_membership_required = excluded.is_membership_required,
    membership_plan_id = excluded.membership_plan_id,
    raw_terms = excluded.raw_terms;

  return new;
end;
$function$;
