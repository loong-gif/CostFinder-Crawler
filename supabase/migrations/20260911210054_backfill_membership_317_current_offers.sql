BEGIN;

-- Current source: https://beautysolutionsmedspa.com/ (Crystal Membership).
UPDATE public.clinic_memberships
   SET benefits = jsonb_build_array(
           'Botox/Dysport $13.50 per unit',
           'Daxxify $10.99 per unit',
           '$50 off all filler syringes',
           '20% off all chemical peels',
           '15% off skincare products, Latisse, and Upneeq'
       ),
       updated_at = now()
 WHERE plan_id = 317;

DO $$
DECLARE
    v_botox_id bigint;
    v_dysport_id bigint;
    v_daxxify_id bigint;
    v_filler_id bigint;
    v_peel_id bigint;
    v_tox_offer_id bigint;
    v_daxxify_offer_id bigint;
    v_filler_offer_id bigint;
    v_peel_offer_id bigint;
BEGIN
    SELECT service_id INTO v_botox_id FROM public.clinic_services
     WHERE business_id = 2199 AND lower(btrim(service_name)) = 'botox' ORDER BY service_id LIMIT 1;
    SELECT service_id INTO v_dysport_id FROM public.clinic_services
     WHERE business_id = 2199 AND lower(btrim(service_name)) = 'dysport' ORDER BY service_id LIMIT 1;
    SELECT service_id INTO v_daxxify_id FROM public.clinic_services
     WHERE business_id = 2199 AND lower(btrim(service_name)) = 'daxxify' ORDER BY service_id LIMIT 1;
    SELECT service_id INTO v_filler_id FROM public.clinic_services
     WHERE business_id = 2199 AND lower(btrim(service_name)) = 'dermal filler' ORDER BY service_id LIMIT 1;
    SELECT service_id INTO v_peel_id FROM public.clinic_services
     WHERE business_id = 2199 AND lower(btrim(service_name)) = 'chemical peel' ORDER BY service_id LIMIT 1;

    IF v_peel_id IS NULL THEN
        INSERT INTO public.clinic_services (business_id, service_name, source_url)
        VALUES (2199, 'Chemical Peel', 'https://beautysolutionsmedspa.com/')
        RETURNING service_id INTO v_peel_id;
    END IF;

    SELECT id INTO v_tox_offer_id FROM public.promo_offer_master
     WHERE membership_plan_id = 317 AND offer_raw_text = 'Crystal Membership: Botox/Dysport $13.50 per unit'
     ORDER BY id LIMIT 1;
    IF v_tox_offer_id IS NULL THEN
        INSERT INTO public.promo_offer_master (business_id, membership_plan_id, discount_price, offer_raw_text, price_model, is_membership_required, is_active, moderation_status)
        VALUES (2199, 317, 13.50, 'Crystal Membership: Botox/Dysport $13.50 per unit', 'per_unit', TRUE, TRUE, 'approved')
        RETURNING id INTO v_tox_offer_id;
    END IF;

    SELECT id INTO v_daxxify_offer_id FROM public.promo_offer_master
     WHERE membership_plan_id = 317 AND offer_raw_text = 'Crystal Membership: Daxxify $10.99 per unit'
     ORDER BY id LIMIT 1;
    IF v_daxxify_offer_id IS NULL THEN
        INSERT INTO public.promo_offer_master (business_id, membership_plan_id, discount_price, offer_raw_text, price_model, is_membership_required, is_active, moderation_status)
        VALUES (2199, 317, 10.99, 'Crystal Membership: Daxxify $10.99 per unit', 'per_unit', TRUE, TRUE, 'approved')
        RETURNING id INTO v_daxxify_offer_id;
    END IF;

    SELECT id INTO v_filler_offer_id FROM public.promo_offer_master
     WHERE membership_plan_id = 317 AND offer_raw_text = 'Crystal Membership: $50 off all filler syringes'
     ORDER BY id LIMIT 1;
    IF v_filler_offer_id IS NULL THEN
        INSERT INTO public.promo_offer_master (business_id, membership_plan_id, discount_amount, offer_raw_text, price_model, is_membership_required, is_active, moderation_status)
        VALUES (2199, 317, 50, 'Crystal Membership: $50 off all filler syringes', 'per_unit', TRUE, TRUE, 'approved')
        RETURNING id INTO v_filler_offer_id;
    END IF;

    SELECT id INTO v_peel_offer_id FROM public.promo_offer_master
     WHERE membership_plan_id = 317 AND offer_raw_text = 'Crystal Membership: 20% off all chemical peels'
     ORDER BY id LIMIT 1;
    IF v_peel_offer_id IS NULL THEN
        INSERT INTO public.promo_offer_master (business_id, membership_plan_id, discount_percent, offer_raw_text, price_model, is_membership_required, is_active, moderation_status)
        VALUES (2199, 317, 20, 'Crystal Membership: 20% off all chemical peels', 'total', TRUE, TRUE, 'approved')
        RETURNING id INTO v_peel_offer_id;
    END IF;

    INSERT INTO public.promo_offer_items (offer_id, service_id, item_name, quantity, unit_price, reason)
    SELECT v_tox_offer_id, v_botox_id, 'Botox', NULL, 13.50, 'Per-unit price; quantity intentionally NULL.'
     WHERE NOT EXISTS (SELECT 1 FROM public.promo_offer_items WHERE offer_id = v_tox_offer_id AND service_id = v_botox_id);
    INSERT INTO public.promo_offer_items (offer_id, service_id, item_name, quantity, unit_price, reason)
    SELECT v_tox_offer_id, v_dysport_id, 'Dysport', NULL, 13.50, 'Per-unit price; quantity intentionally NULL.'
     WHERE NOT EXISTS (SELECT 1 FROM public.promo_offer_items WHERE offer_id = v_tox_offer_id AND service_id = v_dysport_id);
    INSERT INTO public.promo_offer_items (offer_id, service_id, item_name, quantity, unit_price, reason)
    SELECT v_daxxify_offer_id, v_daxxify_id, 'Daxxify', NULL, 10.99, 'Per-unit price; quantity intentionally NULL.'
     WHERE NOT EXISTS (SELECT 1 FROM public.promo_offer_items WHERE offer_id = v_daxxify_offer_id AND service_id = v_daxxify_id);
    INSERT INTO public.promo_offer_items (offer_id, service_id, item_name, quantity, reason)
    SELECT v_filler_offer_id, v_filler_id, 'Dermal Filler', NULL, 'Per-syringe discount; quantity intentionally NULL.'
     WHERE NOT EXISTS (SELECT 1 FROM public.promo_offer_items WHERE offer_id = v_filler_offer_id AND service_id = v_filler_id);
    INSERT INTO public.promo_offer_items (offer_id, service_id, item_name, quantity, reason)
    SELECT v_peel_offer_id, v_peel_id, 'Chemical Peel', NULL, 'Membership-wide percent discount; quantity intentionally NULL.'
     WHERE NOT EXISTS (SELECT 1 FROM public.promo_offer_items WHERE offer_id = v_peel_offer_id AND service_id = v_peel_id);
END;
$$;

UPDATE public.promo_offer_items AS item
   SET reason = CASE
       WHEN offer.offer_raw_text = 'Crystal Membership: $50 off all filler syringes'
           THEN 'Per-syringe discount; quantity intentionally NULL.'
       WHEN offer.offer_raw_text = 'Crystal Membership: 20% off all chemical peels'
           THEN 'Membership-wide percent discount; quantity intentionally NULL.'
       ELSE 'Per-unit price; quantity intentionally NULL.'
   END,
       updated_at = now()
  FROM public.promo_offer_master AS offer
 WHERE item.offer_id = offer.id
   AND offer.membership_plan_id = 317
   AND item.quantity IS NULL;

COMMIT;
