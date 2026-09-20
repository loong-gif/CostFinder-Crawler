BEGIN;

DO $$
DECLARE
    v_service_id bigint;
    v_offer_id bigint;
BEGIN
    SELECT service_id
      INTO v_service_id
      FROM public.clinic_services
     WHERE business_id = 308
       AND lower(btrim(service_name)) = 'skinvive'
     ORDER BY service_id
     LIMIT 1;

    IF v_service_id IS NULL THEN
        INSERT INTO public.clinic_services (business_id, service_name, source_url)
        VALUES (308, 'Skinvive', 'https://www.beautyandhealthbyliz.com/med-spa-specials-tucson')
        RETURNING service_id INTO v_service_id;
    END IF;

    SELECT id
      INTO v_offer_id
      FROM public.promo_offer_master
     WHERE promotion_id = 162
       AND offer_raw_text = 'Enjoy two syringes at $700 ($100 savings)'
     ORDER BY id
     LIMIT 1;

    IF v_offer_id IS NULL THEN
        INSERT INTO public.promo_offer_master (
            business_id,
            promotion_id,
            regular_price,
            discount_price,
            discount_amount,
            offer_raw_text,
            price_model,
            is_active,
            moderation_status
        ) VALUES (
            308,
            162,
            NULL,
            700,
            100,
            'Enjoy two syringes at $700 ($100 savings)',
            'total',
            TRUE,
            'approved'
        )
        RETURNING id INTO v_offer_id;
    END IF;

    INSERT INTO public.promo_offer_items (offer_id, service_id, item_name, quantity)
    SELECT v_offer_id, v_service_id, 'Skinvive', 2
    WHERE NOT EXISTS (
        SELECT 1
        FROM public.promo_offer_items
        WHERE offer_id = v_offer_id
          AND service_id = v_service_id
    );
END;
$$;

COMMIT;
