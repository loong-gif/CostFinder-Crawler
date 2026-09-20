BEGIN;

-- The current source page is headed "Specials | Sep'26" and replaces the
-- stale July end date. It supplies no precise end day, so retain NULL rather
-- than manufacturing one.
UPDATE public.clinic_promotions
   SET campaign_start_date = DATE '2026-09-01',
       campaign_end_date = NULL,
       is_active = TRUE,
       updated_at = now()
 WHERE promotion_id = 149;

DO $$
DECLARE
    v_service_id bigint;
    v_new_patient_offer_id bigint;
    v_returning_offer_id bigint;
BEGIN
    SELECT service_id
      INTO v_service_id
      FROM public.clinic_services
     WHERE business_id = 346
       AND lower(btrim(service_name)) = 'jeuveau'
     ORDER BY service_id
     LIMIT 1;

    IF v_service_id IS NULL THEN
        INSERT INTO public.clinic_services (business_id, service_name, source_url)
        VALUES (346, 'Jeuveau', 'https://www.skinjectables.com/specials')
        RETURNING service_id INTO v_service_id;
    END IF;

    SELECT id
      INTO v_new_patient_offer_id
      FROM public.promo_offer_master
     WHERE promotion_id = 149
       AND offer_raw_text = 'New patients: Jeuveau $8.00/unit; minimum 30 units'
     ORDER BY id
     LIMIT 1;

    IF v_new_patient_offer_id IS NULL THEN
        INSERT INTO public.promo_offer_master (
            business_id, promotion_id, discount_price, offer_raw_text,
            price_model, is_active, is_new_customer_required, moderation_status
        ) VALUES (
            346, 149, 8,
            'New patients: Jeuveau $8.00/unit; minimum 30 units',
            'per_unit', TRUE, TRUE, 'approved'
        )
        RETURNING id INTO v_new_patient_offer_id;
    END IF;

    SELECT id
      INTO v_returning_offer_id
      FROM public.promo_offer_master
     WHERE promotion_id = 149
       AND offer_raw_text = 'Returning patients: Jeuveau $9.00/unit; minimum 30 units'
     ORDER BY id
     LIMIT 1;

    IF v_returning_offer_id IS NULL THEN
        INSERT INTO public.promo_offer_master (
            business_id, promotion_id, discount_price, offer_raw_text,
            price_model, is_active, is_new_customer_required, moderation_status
        ) VALUES (
            346, 149, 9,
            'Returning patients: Jeuveau $9.00/unit; minimum 30 units',
            'per_unit', TRUE, FALSE, 'approved'
        )
        RETURNING id INTO v_returning_offer_id;
    END IF;

    INSERT INTO public.promo_offer_items (offer_id, service_id, item_name, quantity, unit_price)
    SELECT v_new_patient_offer_id, v_service_id, 'Jeuveau', NULL, 8
    WHERE NOT EXISTS (
        SELECT 1 FROM public.promo_offer_items
         WHERE offer_id = v_new_patient_offer_id AND service_id = v_service_id
    );

    INSERT INTO public.promo_offer_items (offer_id, service_id, item_name, quantity, unit_price)
    SELECT v_returning_offer_id, v_service_id, 'Jeuveau', NULL, 9
    WHERE NOT EXISTS (
        SELECT 1 FROM public.promo_offer_items
         WHERE offer_id = v_returning_offer_id AND service_id = v_service_id
    );
END;
$$;

COMMIT;
