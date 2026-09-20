-- Keep future active offers closed over a valid source, service mapping, and
-- concrete per-unit price.
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
            SELECT 1 FROM public.promo_offer_items item
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
        NEW.moderation_status='pending_review';
        IF position('Offer failed activation quality gate' IN coalesce(NEW.moderation_notes,''))=0 THEN
            NEW.moderation_notes=concat_ws(E'\n', NULLIF(NEW.moderation_notes,''), 'Offer failed activation quality gate; retained for manual review');
        END IF;
    END IF;
    RETURN NEW;
END;
$function$;

DROP TRIGGER IF EXISTS trg_prevent_incomplete_offer_activation ON public.promo_offer_master;
CREATE TRIGGER trg_prevent_incomplete_offer_activation
BEFORE INSERT OR UPDATE OF is_active ON public.promo_offer_master
FOR EACH ROW EXECUTE FUNCTION public.prevent_incomplete_offer_activation();

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
        SET is_active=false, moderation_status='pending_review',
            moderation_notes=CASE
                WHEN position('Offer failed item quality gate' IN coalesce(moderation_notes,''))>0 THEN moderation_notes
                ELSE concat_ws(E'\n', NULLIF(moderation_notes,''), 'Offer failed item quality gate; retained for manual review')
            END,
            updated_at=now()
        WHERE id=NEW.offer_id AND is_active;
    END IF;
    RETURN NEW;
END;
$function$;

DROP TRIGGER IF EXISTS trg_deactivate_offer_with_unresolved_item ON public.promo_offer_items;
CREATE TRIGGER trg_deactivate_offer_with_unresolved_item
AFTER INSERT OR UPDATE OF service_id, unit_price, item_name ON public.promo_offer_items
FOR EACH ROW EXECUTE FUNCTION public.deactivate_offer_with_unresolved_item();
