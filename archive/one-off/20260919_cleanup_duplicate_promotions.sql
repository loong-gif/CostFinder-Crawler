-- Soft-clean only unambiguous promotion duplicates.
-- Keep the newer active canonical row; preserve the older row for audit history.

begin;

select pg_advisory_xact_lock(hashtext('costfinder:cleanup_duplicate_promotions:20260919'));

update public.clinic_promotions
set is_active = false
where promotion_id in (2, 857)
  and is_active = true
  and not exists (
    select 1
    from public.promo_offer_master pom
    where pom.promotion_id = clinic_promotions.promotion_id
      and pom.is_active = true
  );

commit;
