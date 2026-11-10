-- 促销与会员行写入时 is_offer_extracted 由数据库按"存在关联 offer"派生，写入语句给出的值一律被覆盖；
-- offer 变化时由 trg_sync_is_offer_extracted 重算（同样经本触发器取值）。
create or replace function public.fn_derive_promotion_is_offer_extracted()
returns trigger
language plpgsql
set search_path to ''
as $$
begin
  new.is_offer_extracted := exists (select 1 from public.promo_offer_master m where m.promotion_id = new.promotion_id);
  return new;
end
$$;

create or replace function public.fn_derive_membership_is_offer_extracted()
returns trigger
language plpgsql
set search_path to ''
as $$
begin
  new.is_offer_extracted := exists (select 1 from public.promo_offer_master m where m.membership_plan_id = new.plan_id);
  return new;
end
$$;

drop trigger if exists trg_derive_is_offer_extracted on public.clinic_promotions;
create trigger trg_derive_is_offer_extracted
before insert or update on public.clinic_promotions
for each row execute function public.fn_derive_promotion_is_offer_extracted();

drop trigger if exists trg_derive_is_offer_extracted on public.clinic_memberships;
create trigger trg_derive_is_offer_extracted
before insert or update on public.clinic_memberships
for each row execute function public.fn_derive_membership_is_offer_extracted();

-- 现有数据：按关联 offer 是否存在重算
update public.clinic_promotions p
   set is_offer_extracted = exists (select 1 from public.promo_offer_master m where m.promotion_id = p.promotion_id)
 where p.is_offer_extracted is distinct from exists (select 1 from public.promo_offer_master m where m.promotion_id = p.promotion_id);
update public.clinic_memberships c
   set is_offer_extracted = exists (select 1 from public.promo_offer_master m where m.membership_plan_id = c.plan_id)
 where c.is_offer_extracted is distinct from exists (select 1 from public.promo_offer_master m where m.membership_plan_id = c.plan_id);
