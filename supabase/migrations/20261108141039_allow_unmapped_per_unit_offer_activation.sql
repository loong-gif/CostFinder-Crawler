CREATE OR REPLACE FUNCTION public.prevent_incomplete_offer_activation()
RETURNS trigger
LANGUAGE plpgsql
SET search_path = public
AS $function$
BEGIN
    IF NEW.is_active AND (
        (NEW.promotion_id IS NULL) = (NEW.membership_plan_id IS NULL)
        OR (NEW.promotion_id IS NOT NULL AND NOT EXISTS (
            SELECT 1 FROM public.clinic_promotions AS promotion
            WHERE promotion.promotion_id = NEW.promotion_id
              AND promotion.business_id = NEW.business_id
              AND promotion.is_active
              AND (promotion.campaign_end_date IS NULL
                   OR promotion.campaign_end_date >= current_date)
        ))
        OR (NEW.membership_plan_id IS NOT NULL AND NOT EXISTS (
            SELECT 1 FROM public.clinic_memberships AS membership
            WHERE membership.plan_id = NEW.membership_plan_id
              AND membership.business_id = NEW.business_id
        ))
        OR NOT EXISTS (
            SELECT 1 FROM public.promo_offer_items AS item
            WHERE item.offer_id = NEW.id
        )
        OR EXISTS (
            SELECT 1
              FROM public.promo_offer_items AS item
              LEFT JOIN public.clinic_services AS service
                ON service.service_id = item.service_id
             WHERE item.offer_id = NEW.id
               AND (btrim(coalesce(item.item_name, '')) = ''
                    OR (item.service_id IS NOT NULL
                        AND (service.service_id IS NULL
                             OR service.business_id IS DISTINCT FROM NEW.business_id)))
        )
        OR (
            NEW.price_model = 'per_unit'
            AND (
                NEW.discount_price IS NULL
                OR EXISTS (
                    SELECT 1 FROM public.promo_offer_items AS item
                    WHERE item.offer_id = NEW.id
                      AND (item.unit_price IS NULL
                           OR round(item.unit_price, 2)
                              IS DISTINCT FROM round(NEW.discount_price, 2))
                )
            )
        )
        OR (
            NEW.is_package = false
            AND NEW.discount_price IS NOT NULL
            AND EXISTS (
                SELECT 1 FROM public.promo_offer_items AS item
                WHERE item.offer_id = NEW.id
                  AND (item.unit_price IS NULL
                       OR round(
                           CASE WHEN NEW.price_model = 'per_unit'
                                THEN item.unit_price
                                ELSE item.unit_price * coalesce(item.delivered_units, 1)
                           END,
                           2
                       ) IS DISTINCT FROM round(NEW.discount_price, 2))
            )
        )
    ) THEN
        NEW.is_active := false;
    END IF;
    RETURN NEW;
END;
$function$;
