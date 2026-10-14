-- QA denormalized export (one row per offer, aggregated items + source fields)
SELECT
  NULL::text AS qa_status,
  NULL::text AS qa_notes,
  NULL::boolean AS col_c,
  NULL::text AS col_d,
  m.id AS offer_id,
  m.business_id,
  m.promotion_id,
  m.membership_plan_id,
  CASE
    WHEN m.membership_plan_id IS NOT NULL THEN 'membership'
    WHEN m.promotion_id IS NOT NULL THEN 'promotion'
    ELSE 'business'
  END AS source_type,
  coalesce(p.business_id, cm.business_id, m.business_id) AS source_business_id,
  coalesce(p.promotion_title, cm.membership_name) AS source_title,
  coalesce(p.source_url, cm.source_url) AS source_url,
  agg.item_names,
  m.offer_raw_text,
  m.regular_price,
  m.discount_price,
  m.discount_percent,
  m.discount_amount,
  agg.service_ids,
  agg.matched_service_names,
  agg.service_business_ids,
  agg.quantities,
  agg.unit_prices,
  m.is_membership_required,
  coalesce(agg.item_count, 0) AS item_count,
  agg.offer_item_ids,
  agg.service_categories,
  CASE
    WHEN m.conditions IS NULL OR cardinality(m.conditions) = 0 THEN NULL
    ELSE (SELECT json_agg(elem)::text FROM unnest(m.conditions) AS elem)
  END AS conditions,
  coalesce(p.promotion_content, cm.benefits)::text AS source_content,
  agg.service_regular_prices,
  agg.service_unit_types,
  agg.service_areas,
  agg.service_source_urls,
  p.campaign_start_date AS source_start_date,
  p.campaign_end_date AS source_end_date,
  cm.membership_price AS source_membership_price,
  cm.billing_period AS source_billing_period,
  cm.minimum_commitment_months AS source_minimum_commitment_months
FROM public.promo_offer_master m
LEFT JOIN public.clinic_promotions p ON p.promotion_id = m.promotion_id
LEFT JOIN public.clinic_memberships cm ON cm.plan_id = m.membership_plan_id
LEFT JOIN LATERAL (
  SELECT
    count(*) AS item_count,
    string_agg(coalesce(i.item_name, ''), E'\n' ORDER BY i.offer_item_id) AS item_names,
    string_agg(coalesce(i.service_id::text, ''), E'\n' ORDER BY i.offer_item_id) AS service_ids,
    string_agg(coalesce(s.service_name, ''), E'\n' ORDER BY i.offer_item_id) AS matched_service_names,
    string_agg(coalesce(s.business_id::text, ''), E'\n' ORDER BY i.offer_item_id) AS service_business_ids,
    string_agg(
      CASE WHEN i.quantity IS NULL THEN '' ELSE i.quantity::text END,
      E'\n' ORDER BY i.offer_item_id
    ) AS quantities,
    string_agg(
      CASE WHEN i.unit_price IS NULL THEN '' ELSE i.unit_price::text END,
      E'\n' ORDER BY i.offer_item_id
    ) AS unit_prices,
    string_agg(i.offer_item_id::text, E'\n' ORDER BY i.offer_item_id) AS offer_item_ids,
    string_agg(coalesce(s.service_category, ''), E'\n' ORDER BY i.offer_item_id) AS service_categories,
    string_agg(
      CASE WHEN s.regular_price IS NULL THEN '' ELSE s.regular_price::text END,
      E'\n' ORDER BY i.offer_item_id
    ) AS service_regular_prices,
    string_agg(coalesce(s.unit_type, ''), E'\n' ORDER BY i.offer_item_id) AS service_unit_types,
    string_agg(coalesce(s.service_area, ''), E'\n' ORDER BY i.offer_item_id) AS service_areas,
    string_agg(coalesce(s.source_url, ''), E'\n' ORDER BY i.offer_item_id) AS service_source_urls
  FROM public.promo_offer_items i
  LEFT JOIN public.clinic_services s ON s.service_id = i.service_id
  WHERE i.offer_id = m.id
) agg ON true
ORDER BY m.id;
