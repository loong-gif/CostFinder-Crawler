BEGIN;

-- Remove the two policies' column dependencies while preserving their access scope.
DROP POLICY IF EXISTS "Owners can create deals for their business" ON public.promo_offer_master;
CREATE POLICY "Owners can create deals for their business"
ON public.promo_offer_master
FOR INSERT TO authenticated
WITH CHECK (
    EXISTS (
        SELECT 1
        FROM public.business_profiles bp
        WHERE bp.id=auth.uid()
          AND bp.business_id=promo_offer_master.business_id
          AND bp.claim_status='approved'
    )
);

DROP POLICY IF EXISTS "Public can read active approved offers" ON public.promo_offer_master;
CREATE POLICY "Public can read active approved offers"
ON public.promo_offer_master
FOR SELECT TO anon, authenticated
USING (is_active=true);

-- Remove the moderation-only write guard before dropping the fields it protects.
DROP TRIGGER IF EXISTS trg_guard_deal_moderation_status ON public.promo_offer_master;
DROP FUNCTION IF EXISTS public.guard_deal_moderation_status();

-- Keep the data-quality activation guards, but no longer write moderation fields.
CREATE OR REPLACE FUNCTION public.prevent_incomplete_offer_activation()
RETURNS trigger
LANGUAGE plpgsql
SET search_path = public
AS $function$
BEGIN
    IF NEW.is_active AND (
        (NEW.promotion_id IS NULL) = (NEW.membership_plan_id IS NULL)
        OR (NEW.promotion_id IS NOT NULL AND NOT EXISTS (
            SELECT 1 FROM public.clinic_promotions promotion
            WHERE promotion.promotion_id=NEW.promotion_id AND promotion.business_id=NEW.business_id
              AND promotion.is_active AND (promotion.campaign_end_date IS NULL OR promotion.campaign_end_date>=current_date)
        ))
        OR (NEW.membership_plan_id IS NOT NULL AND NOT EXISTS (
            SELECT 1 FROM public.clinic_memberships membership
            WHERE membership.plan_id=NEW.membership_plan_id AND membership.business_id=NEW.business_id
        ))
        OR NOT EXISTS (SELECT 1 FROM public.promo_offer_items item WHERE item.offer_id=NEW.id)
        OR EXISTS (
            SELECT 1
            FROM public.promo_offer_items item
            LEFT JOIN public.clinic_services service ON service.service_id=item.service_id
            WHERE item.offer_id=NEW.id
              AND (item.service_id IS NULL OR service.service_id IS NULL
                   OR service.business_id IS DISTINCT FROM NEW.business_id
                   OR item.item_name IS DISTINCT FROM service.service_name)
        )
        OR (NEW.price_model='per_unit' AND (
            NEW.discount_price IS NULL OR EXISTS (
                SELECT 1 FROM public.promo_offer_items item
                WHERE item.offer_id=NEW.id
                  AND (item.unit_price IS NULL OR abs(item.unit_price-NEW.discount_price)>0.011)
            )
        ))
    ) THEN
        NEW.is_active=false;
    END IF;
    RETURN NEW;
END;
$function$;

CREATE OR REPLACE FUNCTION public.deactivate_offer_with_unresolved_item()
RETURNS trigger
LANGUAGE plpgsql
SET search_path = public
AS $function$
BEGIN
    IF EXISTS (
        SELECT 1 FROM public.promo_offer_master offer
        LEFT JOIN public.clinic_services service ON service.service_id=NEW.service_id
        WHERE offer.id=NEW.offer_id AND offer.is_active
          AND (NEW.service_id IS NULL OR service.service_id IS NULL
               OR service.business_id IS DISTINCT FROM offer.business_id
               OR NEW.item_name IS DISTINCT FROM service.service_name
               OR (offer.price_model='per_unit' AND (
                   offer.discount_price IS NULL OR NEW.unit_price IS NULL
                   OR abs(NEW.unit_price-offer.discount_price)>0.011
               )))
    ) THEN
        UPDATE public.promo_offer_master
        SET is_active=false, updated_at=now()
        WHERE id=NEW.offer_id AND is_active;
    END IF;
    RETURN NEW;
END;
$function$;

DROP INDEX IF EXISTS public.idx_promo_offer_master_moderation_status;
ALTER TABLE public.promo_offer_master
    DROP COLUMN IF EXISTS moderation_status,
    DROP COLUMN IF EXISTS moderation_notes;

COMMIT;
