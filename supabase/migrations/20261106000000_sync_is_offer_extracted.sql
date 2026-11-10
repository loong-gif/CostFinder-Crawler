-- is_offer_extracted 由 promo_offer_master 的关联行自动维护：存在关联 offer 为 true，不存在为 false。
create or replace function public.fn_sync_is_offer_extracted()
returns trigger
language plpgsql
set search_path to ''
as $$
begin
  if tg_op in ('UPDATE', 'DELETE') then
    update public.clinic_promotions p
       set is_offer_extracted = exists (select 1 from public.promo_offer_master m where m.promotion_id = p.promotion_id)
     where p.promotion_id = old.promotion_id
       and p.is_offer_extracted is distinct from exists (select 1 from public.promo_offer_master m where m.promotion_id = p.promotion_id);
    update public.clinic_memberships c
       set is_offer_extracted = exists (select 1 from public.promo_offer_master m where m.membership_plan_id = c.plan_id)
     where c.plan_id = old.membership_plan_id
       and c.is_offer_extracted is distinct from exists (select 1 from public.promo_offer_master m where m.membership_plan_id = c.plan_id);
  end if;
  if tg_op in ('INSERT', 'UPDATE') then
    update public.clinic_promotions p
       set is_offer_extracted = exists (select 1 from public.promo_offer_master m where m.promotion_id = p.promotion_id)
     where p.promotion_id = new.promotion_id
       and p.is_offer_extracted is distinct from exists (select 1 from public.promo_offer_master m where m.promotion_id = p.promotion_id);
    update public.clinic_memberships c
       set is_offer_extracted = exists (select 1 from public.promo_offer_master m where m.membership_plan_id = c.plan_id)
     where c.plan_id = new.membership_plan_id
       and c.is_offer_extracted is distinct from exists (select 1 from public.promo_offer_master m where m.membership_plan_id = c.plan_id);
  end if;
  return null;
end
$$;

drop trigger if exists trg_sync_is_offer_extracted on public.promo_offer_master;
create trigger trg_sync_is_offer_extracted
after insert or update of promotion_id, membership_plan_id or delete on public.promo_offer_master
for each row execute function public.fn_sync_is_offer_extracted();

-- 现有数据：已有关联 offer 的标记为 true
update public.clinic_promotions p
   set is_offer_extracted = true
 where p.is_offer_extracted is distinct from true
   and exists (select 1 from public.promo_offer_master m where m.promotion_id = p.promotion_id);
update public.clinic_memberships c
   set is_offer_extracted = true
 where c.is_offer_extracted is distinct from true
   and exists (select 1 from public.promo_offer_master m where m.membership_plan_id = c.plan_id);
