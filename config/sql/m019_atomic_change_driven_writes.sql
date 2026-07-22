-- M019: atomic RPC writers for change-driven monitor extraction.
-- Preflight: M006 change-event tables and M009 promo_offer_items must exist.

BEGIN;

DO $$
BEGIN
    IF to_regclass('public.promo_offer_change_events') IS NULL
       OR to_regclass('public.promo_offer_match_candidates') IS NULL THEN
        RAISE EXCEPTION
            'M019 requires M006 promo_offer_change_events and promo_offer_match_candidates';
    END IF;

    IF to_regclass('public.promo_offer_items') IS NULL THEN
        RAISE EXCEPTION 'M019 requires M009 promo_offer_items';
    END IF;

    IF to_regclass('public.promo_offer_master') IS NULL THEN
        RAISE EXCEPTION 'M019 requires public.promo_offer_master';
    END IF;
END $$;

CREATE OR REPLACE FUNCTION public.persist_promo_offer_change_events(
    p_events JSONB,
    p_match_candidates JSONB DEFAULT '[]'::JSONB
)
RETURNS JSONB
LANGUAGE plpgsql
SECURITY DEFINER
SET search_path = public
AS $$
DECLARE
    v_event JSONB;
    v_candidate JSONB;
    v_events_inserted INTEGER := 0;
    v_candidates_inserted INTEGER := 0;
BEGIN
    IF p_events IS NULL OR jsonb_typeof(p_events) <> 'array' THEN
        RAISE EXCEPTION 'p_events must be a JSON array';
    END IF;

    IF p_match_candidates IS NULL OR jsonb_typeof(p_match_candidates) <> 'array' THEN
        RAISE EXCEPTION 'p_match_candidates must be a JSON array';
    END IF;

    FOR v_event IN SELECT value FROM jsonb_array_elements(p_events) AS t(value)
    LOOP
        INSERT INTO promo_offer_change_events (
            change_event_id,
            promo_website_id,
            source_url,
            source_url_normalized,
            business_id,
            crawl_run_id,
            monitor_event_id,
            diff_type,
            business_change_type,
            affected_segment_ids,
            before_text,
            after_text,
            before_hash,
            after_hash,
            proposed_action,
            target_offer_id,
            proposed_field_updates,
            proposed_new_offer,
            confidence,
            confidence_label,
            reason,
            validator_status,
            validator_errors
        ) VALUES (
            COALESCE(NULLIF(v_event->>'change_event_id', '')::UUID, gen_random_uuid()),
            NULLIF(v_event->>'promo_website_id', '')::BIGINT,
            COALESCE(v_event->>'source_url', ''),
            COALESCE(v_event->>'source_url_normalized', ''),
            NULLIF(v_event->>'business_id', '')::BIGINT,
            NULLIF(v_event->>'crawl_run_id', '')::UUID,
            NULLIF(v_event->>'monitor_event_id', ''),
            NULLIF(v_event->>'diff_type', ''),
            COALESCE(NULLIF(v_event->>'business_change_type', ''), 'unknown'),
            COALESCE(
                ARRAY(SELECT jsonb_array_elements_text(v_event->'affected_segment_ids'))::UUID[],
                '{}'::UUID[]
            ),
            v_event->>'before_text',
            v_event->>'after_text',
            NULLIF(v_event->>'before_hash', ''),
            NULLIF(v_event->>'after_hash', ''),
            COALESCE(v_event->>'proposed_action', 'insert_offer'),
            NULLIF(v_event->>'target_offer_id', '')::BIGINT,
            COALESCE(v_event->'proposed_field_updates', '{}'::JSONB),
            COALESCE(v_event->'proposed_new_offer', '{}'::JSONB),
            NULLIF(v_event->>'confidence', '')::NUMERIC,
            NULLIF(v_event->>'confidence_label', ''),
            NULLIF(v_event->>'reason', ''),
            COALESCE(NULLIF(v_event->>'validator_status', ''), 'pending'),
            COALESCE(v_event->'validator_errors', '[]'::JSONB)
        );
        v_events_inserted := v_events_inserted + 1;
    END LOOP;

    FOR v_candidate IN SELECT value FROM jsonb_array_elements(p_match_candidates) AS t(value)
    LOOP
        INSERT INTO promo_offer_match_candidates (
            change_event_id,
            segment_id,
            candidate_offer_id,
            match_score,
            match_method,
            score_breakdown,
            rank,
            is_selected
        ) VALUES (
            (v_candidate->>'change_event_id')::UUID,
            NULLIF(v_candidate->>'segment_id', '')::UUID,
            NULLIF(v_candidate->>'candidate_offer_id', '')::BIGINT,
            COALESCE(NULLIF(v_candidate->>'match_score', '')::NUMERIC, 0),
            COALESCE(v_candidate->>'match_method', 'unknown'),
            COALESCE(v_candidate->'score_breakdown', '{}'::JSONB),
            NULLIF(v_candidate->>'rank', '')::INTEGER,
            COALESCE((v_candidate->>'is_selected')::BOOLEAN, FALSE)
        );
        v_candidates_inserted := v_candidates_inserted + 1;
    END LOOP;

    RETURN jsonb_build_object(
        'ok', TRUE,
        'change_events_inserted', v_events_inserted,
        'match_candidates_inserted', v_candidates_inserted
    );
END;
$$;

CREATE OR REPLACE FUNCTION public.apply_promo_change_offer_action(
    p_action JSONB,
    p_now TIMESTAMPTZ DEFAULT NOW()
)
RETURNS JSONB
LANGUAGE plpgsql
SECURITY DEFINER
SET search_path = public
AS $$
DECLARE
    v_action TEXT;
    v_offer_id BIGINT;
    v_master JSONB;
    v_items JSONB;
    v_item JSONB;
    v_new_id BIGINT;
    v_item_name TEXT;
BEGIN
    IF p_action IS NULL OR jsonb_typeof(p_action) <> 'object' THEN
        RETURN jsonb_build_object('ok', FALSE, 'error', 'p_action must be a JSON object');
    END IF;

    v_action := lower(btrim(COALESCE(p_action->>'action', '')));
    v_master := COALESCE(p_action->'master', '{}'::JSONB);
    v_items := COALESCE(p_action->'items', '[]'::JSONB);

    IF v_action = 'mark_ended' THEN
        v_offer_id := NULLIF(p_action->>'offer_id', '')::BIGINT;
        IF v_offer_id IS NULL THEN
            RETURN jsonb_build_object('ok', FALSE, 'error', 'missing_offer_id');
        END IF;

        UPDATE promo_offer_master
           SET is_active = FALSE,
               updated_at = p_now,
               last_verified_at = p_now
         WHERE id = v_offer_id;

        IF NOT FOUND THEN
            RETURN jsonb_build_object(
                'ok', FALSE,
                'error', 'offer_not_found',
                'offer_id', v_offer_id
            );
        END IF;

        RETURN jsonb_build_object(
            'ok', TRUE,
            'action', 'mark_ended',
            'offer_id', v_offer_id
        );
    END IF;

    IF v_action = 'update' THEN
        v_offer_id := NULLIF(p_action->>'offer_id', '')::BIGINT;
        IF v_offer_id IS NULL THEN
            RETURN jsonb_build_object('ok', FALSE, 'error', 'missing_offer_id');
        END IF;

        UPDATE promo_offer_master
           SET offer_raw_text = CASE
                   WHEN v_master ? 'offer_raw_text' THEN v_master->>'offer_raw_text'
                   ELSE offer_raw_text
               END,
               service_category = CASE
                   WHEN v_master ? 'service_category' THEN v_master->>'service_category'
                   ELSE service_category
               END,
               regular_price = CASE
                   WHEN v_master ? 'regular_price' THEN NULLIF(v_master->>'regular_price', '')::NUMERIC
                   ELSE regular_price
               END,
               discount_price = CASE
                   WHEN v_master ? 'discount_price' THEN NULLIF(v_master->>'discount_price', '')::NUMERIC
                   ELSE discount_price
               END,
               discount_amount = CASE
                   WHEN v_master ? 'discount_amount' THEN NULLIF(v_master->>'discount_amount', '')::NUMERIC
                   ELSE discount_amount
               END,
               discount_percent = CASE
                   WHEN v_master ? 'discount_percent' THEN NULLIF(v_master->>'discount_percent', '')::NUMERIC
                   ELSE discount_percent
               END,
               is_membership_required = CASE
                   WHEN v_master ? 'is_membership_required'
                       THEN (v_master->>'is_membership_required')::BOOLEAN
                   ELSE is_membership_required
               END,
               is_new_customer_required = CASE
                   WHEN v_master ? 'is_new_customer_required'
                       THEN (v_master->>'is_new_customer_required')::BOOLEAN
                   ELSE is_new_customer_required
               END,
               updated_at = p_now,
               last_verified_at = p_now
         WHERE id = v_offer_id;

        IF NOT FOUND THEN
            RETURN jsonb_build_object(
                'ok', FALSE,
                'error', 'offer_not_found',
                'offer_id', v_offer_id
            );
        END IF;

        DELETE FROM promo_offer_items WHERE offer_id = v_offer_id;

        FOR v_item IN SELECT value FROM jsonb_array_elements(v_items) AS t(value)
        LOOP
            v_item_name := NULLIF(btrim(v_item->>'item_name'), '');
            IF v_item_name IS NULL THEN
                CONTINUE;
            END IF;
            INSERT INTO promo_offer_items (
                offer_id,
                service_id,
                item_name,
                quantity,
                unit_type,
                service_area,
                created_at,
                updated_at
            ) VALUES (
                v_offer_id,
                NULLIF(v_item->>'service_id', '')::BIGINT,
                v_item_name,
                NULLIF(v_item->>'quantity', '')::NUMERIC,
                NULLIF(v_item->>'unit_type', ''),
                NULLIF(v_item->>'service_area', ''),
                p_now,
                p_now
            );
        END LOOP;

        RETURN jsonb_build_object(
            'ok', TRUE,
            'action', 'update',
            'offer_id', v_offer_id
        );
    END IF;

    IF v_action = 'insert' THEN
        INSERT INTO promo_offer_master (
            business_id,
            promotion_id,
            offer_raw_text,
            service_category,
            regular_price,
            discount_price,
            discount_amount,
            discount_percent,
            is_membership_required,
            is_new_customer_required,
            is_active,
            offer_fingerprint,
            offer_type,
            price_model,
            created_at,
            updated_at,
            last_verified_at
        ) VALUES (
            NULLIF(v_master->>'business_id', '')::BIGINT,
            NULLIF(v_master->>'promotion_id', '')::BIGINT,
            COALESCE(v_master->>'offer_raw_text', ''),
            NULLIF(v_master->>'service_category', ''),
            NULLIF(v_master->>'regular_price', '')::NUMERIC,
            NULLIF(v_master->>'discount_price', '')::NUMERIC,
            NULLIF(v_master->>'discount_amount', '')::NUMERIC,
            NULLIF(v_master->>'discount_percent', '')::NUMERIC,
            COALESCE((v_master->>'is_membership_required')::BOOLEAN, FALSE),
            COALESCE((v_master->>'is_new_customer_required')::BOOLEAN, TRUE),
            COALESCE((v_master->>'is_active')::BOOLEAN, TRUE),
            NULLIF(v_master->>'offer_fingerprint', ''),
            COALESCE(NULLIF(v_master->>'offer_type', ''), 'single'),
            COALESCE(NULLIF(v_master->>'price_model', ''), 'total'),
            p_now,
            p_now,
            p_now
        )
        RETURNING id INTO v_new_id;

        FOR v_item IN SELECT value FROM jsonb_array_elements(v_items) AS t(value)
        LOOP
            v_item_name := NULLIF(btrim(v_item->>'item_name'), '');
            IF v_item_name IS NULL THEN
                CONTINUE;
            END IF;
            INSERT INTO promo_offer_items (
                offer_id,
                service_id,
                item_name,
                quantity,
                unit_type,
                service_area,
                created_at,
                updated_at
            ) VALUES (
                v_new_id,
                NULLIF(v_item->>'service_id', '')::BIGINT,
                v_item_name,
                NULLIF(v_item->>'quantity', '')::NUMERIC,
                NULLIF(v_item->>'unit_type', ''),
                NULLIF(v_item->>'service_area', ''),
                p_now,
                p_now
            );
        END LOOP;

        RETURN jsonb_build_object(
            'ok', TRUE,
            'action', 'insert',
            'offer_id', v_new_id
        );
    END IF;

    RETURN jsonb_build_object('ok', FALSE, 'error', 'invalid_action');
EXCEPTION
    WHEN OTHERS THEN
        RETURN jsonb_build_object('ok', FALSE, 'error', SQLERRM);
END;
$$;

COMMIT;
