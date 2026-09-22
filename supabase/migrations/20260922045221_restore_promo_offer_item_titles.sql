begin;

-- The source-backed candidate review for this migration found no rows that
-- still need restoration: the confirmed candidates were already source-facing
-- in the production read-only snapshot. Keep the mapping explicit (rather
-- than inferring from item_name = service_name) so a later candidate can be
-- reviewed and added safely.
create temporary table _restore_promo_offer_item_names (
  offer_item_id bigint primary key,
  offer_id bigint not null,
  expected_current_name text not null,
  restored_item_name text not null check (
    btrim(restored_item_name) <> ''
    and restored_item_name <> expected_current_name
  ),
  source_type text not null,
  source_id bigint
) on commit drop;

-- Intentionally empty. The 2026-09-22 source review classified all confirmed
-- candidates as ALREADY_RESTORED; ambiguous rows were deliberately excluded.

do $function$
declare
  v_invalid_rows bigint;
begin
  select count(*)
    into v_invalid_rows
    from _restore_promo_offer_item_names r
    left join public.promo_offer_items i on i.offer_item_id = r.offer_item_id
   where i.offer_item_id is null
      or i.offer_id is distinct from r.offer_id
      or i.item_name is distinct from r.expected_current_name
      or nullif(btrim(r.restored_item_name), '') is null;

  if v_invalid_rows <> 0 then
    raise exception 'promo offer item title restoration precondition failed for % row(s)', v_invalid_rows;
  end if;
end;
$function$;

create temporary table _restore_promo_offer_item_preimages on commit drop as
select
  i.offer_item_id,
  i.offer_id,
  i.service_id,
  i.delivered_units,
  i.unit_price,
  m.is_active,
  m.regular_price,
  m.discount_price,
  m.discount_amount,
  m.discount_percent,
  m.offer_fingerprint
from _restore_promo_offer_item_names r
join public.promo_offer_items i on i.offer_item_id = r.offer_item_id
join public.promo_offer_master m on m.id = i.offer_id;

create temporary table _restored_promo_offer_item_names on commit drop as
with updated as (
  update public.promo_offer_items i
     set item_name = r.restored_item_name,
         updated_at = now()
    from _restore_promo_offer_item_names r
   where i.offer_item_id = r.offer_item_id
     and i.offer_id = r.offer_id
     and i.item_name = r.expected_current_name
  returning i.offer_item_id, i.offer_id, i.item_name, i.service_id
)
select * from updated;

do $function$
declare
  v_mapping_rows bigint;
  v_updated_rows bigint;
  v_remaining_old_names bigint;
  v_unexpected_changes bigint;
begin
  select count(*) into v_mapping_rows from _restore_promo_offer_item_names;
  select count(*) into v_updated_rows from _restored_promo_offer_item_names;

  if v_updated_rows <> v_mapping_rows then
    raise exception 'promo offer item title restoration updated % rows; expected %', v_updated_rows, v_mapping_rows;
  end if;

  select count(*)
    into v_remaining_old_names
    from _restore_promo_offer_item_names r
    join public.promo_offer_items i on i.offer_item_id = r.offer_item_id
   where i.item_name is distinct from r.restored_item_name;

  if v_remaining_old_names <> 0 then
    raise exception 'promo offer item title restoration left % row(s) unchanged', v_remaining_old_names;
  end if;

  select count(*)
    into v_unexpected_changes
    from _restore_promo_offer_item_preimages p
    join public.promo_offer_items i on i.offer_item_id = p.offer_item_id
    join public.promo_offer_master m on m.id = i.offer_id
   where i.offer_id is distinct from p.offer_id
      or i.service_id is distinct from p.service_id
      or i.delivered_units is distinct from p.delivered_units
      or i.unit_price is distinct from p.unit_price
      or m.is_active is distinct from p.is_active
      or m.regular_price is distinct from p.regular_price
      or m.discount_price is distinct from p.discount_price
      or m.discount_amount is distinct from p.discount_amount
      or m.discount_percent is distinct from p.discount_percent
      or m.offer_fingerprint is distinct from p.offer_fingerprint;

  if v_unexpected_changes <> 0 then
    raise exception 'promo offer item title restoration changed protected fields for % row(s)', v_unexpected_changes;
  end if;
end;
$function$;

commit;
