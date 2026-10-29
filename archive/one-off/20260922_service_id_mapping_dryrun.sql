-- Read-only service_id repair dry run.
-- Generated from the live costfinder Supabase project on 2026-09-22.
-- This file contains SELECT statements only. It does not modify Supabase.
--
-- Rule: source evidence -> canonical service -> exactly one same-business
-- clinic_services row. If the source is scope-only, or the concrete canonical
-- target is missing/ambiguous, the safe proposed value is NULL.

-- A. One row per currently brand-specific mapping that lacks any direct
-- source evidence for that brand/service. The expected live snapshot is
-- 147 rows: 46 active and 101 inactive. Every returned row proposes SET NULL.
WITH evidence AS (
  SELECT
    i.offer_item_id,
    i.offer_id,
    i.service_id,
    i.item_name,
    m.business_id,
    m.is_active,
    m.promotion_id,
    m.membership_plan_id,
    m.offer_raw_text,
    lower(concat_ws(
      ' ',
      m.offer_raw_text,
      coalesce(p.promotion_content::text, ''),
      coalesce(cm.benefits::text, '')
    )) AS evidence_text,
    s.service_name::text AS current_service
  FROM promo_offer_items AS i
  JOIN promo_offer_master AS m ON m.id = i.offer_id
  JOIN clinic_services AS s ON s.service_id = i.service_id
  LEFT JOIN clinic_promotions AS p ON p.promotion_id = m.promotion_id
  LEFT JOIN clinic_memberships AS cm ON cm.plan_id = m.membership_plan_id
  WHERE i.service_id IS NOT NULL
), candidates AS (
  SELECT *,
    CASE current_service
      WHEN 'Botox' THEN 'botox|botulinum'
      WHEN 'Dysport' THEN 'dysport'
      WHEN 'Sculptra' THEN 'sculptra'
      WHEN 'Xeomin' THEN 'xeomin'
      WHEN 'Jeuveau' THEN 'jeuveau'
      WHEN 'Hydrafacial' THEN 'hydrafacial'
      WHEN 'Juvéderm' THEN 'juv[eé]derm'
      WHEN 'Daxxify' THEN 'daxxify'
      WHEN 'ClearSilk' THEN 'clear[ -]?silk'
      WHEN 'Laser Genesis' THEN 'laser genesis'
      WHEN 'Radiesse' THEN 'radiesse'
      WHEN 'Restylane' THEN 'restylane'
      WHEN 'Halo Laser' THEN 'halo'
      WHEN 'IPL Photofacial' THEN 'ipl|photofacial'
      WHEN 'Moxi Laser' THEN 'moxi'
      WHEN 'Skinvive' THEN 'skinvive'
    END AS support_pattern
  FROM evidence
  WHERE current_service IN (
    'Botox', 'Dysport', 'Sculptra', 'Xeomin', 'Jeuveau', 'Hydrafacial',
    'Juvéderm', 'Daxxify', 'ClearSilk', 'Laser Genesis', 'Radiesse',
    'Restylane', 'Halo Laser', 'IPL Photofacial', 'Moxi Laser', 'Skinvive'
  )
)
SELECT
  offer_item_id,
  offer_id,
  is_active,
  business_id,
  service_id AS current_service_id,
  current_service,
  item_name,
  'NULL'::bigint AS proposed_service_id,
  'SET service_id = NULL' AS proposed_action,
  CASE
    WHEN evidence_text ~* '(\\m(tox(ins)?|neuromodulator|injectables?|filler|biostimulator|all services|any service|all products|all treatments|gift card|skin ?care products|laser treatments?)\\M)'
      THEN 'Scope/umbrella wording; no specific-service evidence'
    ELSE 'No evidence for current service in the offer or its linked source'
  END AS rationale,
  left(offer_raw_text, 180) AS source_excerpt,
  CASE
    WHEN is_active THEN 'Existing trigger would deactivate this offer'
    ELSE 'Offer is already inactive'
  END AS operational_effect
FROM candidates
WHERE NOT evidence_text ~ support_pattern
ORDER BY is_active DESC, offer_id, offer_item_id;

-- B. The explicitly reviewed specific-to-generic/wrong-service cases.
-- The target service is named by the source. None has exactly one matching
-- clinic_services row in the same business. Offer 8273 names a multi-brand
-- choice, so the single item cannot safely select the one available Restylane
-- row. Every returned row therefore also proposes SET NULL, not a rebind.
WITH reviewed(offer_item_id, source_canonical_target) AS (
  VALUES
    (1653::bigint, 'Versa'),
    (5348::bigint, 'PDRN'),
    (5350::bigint, 'PDRN'),
    (2190::bigint, 'MULTI: Juvéderm / Restylane'),
    (3529::bigint, 'HIFU'),
    (3722::bigint, 'EZ Gel'),
    (3726::bigint, 'EZ Gel'),
    (4492::bigint, 'PRX Skin Perfecting'),
    (4493::bigint, 'PRFM'),
    (5197::bigint, 'Forma')
), current_rows AS (
  SELECT
    r.source_canonical_target,
    i.offer_item_id,
    i.offer_id,
    i.service_id,
    i.item_name,
    m.business_id,
    m.is_active,
    m.offer_raw_text,
    s.service_name::text AS current_service
  FROM reviewed AS r
  JOIN promo_offer_items AS i USING (offer_item_id)
  JOIN promo_offer_master AS m ON m.id = i.offer_id
  JOIN clinic_services AS s ON s.service_id = i.service_id
)
SELECT
  c.offer_item_id,
  c.offer_id,
  c.is_active,
  c.business_id,
  c.service_id AS current_service_id,
  c.current_service,
  c.item_name,
  c.source_canonical_target,
  'NULL'::bigint AS proposed_service_id,
  'SET service_id = NULL' AS proposed_action,
  'Source is specific, but no unique same-business target service exists' AS rationale,
  c.offer_raw_text AS source_evidence,
  coalesce(
    json_agg(json_build_object('service_id', cs.service_id, 'service_name', cs.service_name::text))
      FILTER (WHERE cs.service_id IS NOT NULL),
    '[]'::json
  ) AS same_business_target_candidates,
  'Existing trigger would deactivate this offer' AS operational_effect
FROM current_rows AS c
LEFT JOIN clinic_services AS cs
  ON cs.business_id = c.business_id
 AND cs.service_name::text = ANY (
   CASE c.source_canonical_target
     WHEN 'MULTI: Juvéderm / Restylane' THEN ARRAY['Juvéderm', 'Restylane']
     ELSE ARRAY[c.source_canonical_target]
   END
 )
GROUP BY
  c.offer_item_id, c.offer_id, c.is_active, c.business_id, c.service_id,
  c.current_service, c.item_name, c.source_canonical_target, c.offer_raw_text
ORDER BY c.offer_id, c.offer_item_id;

-- C. Pre-apply checks. They are deliberately read-only here.
-- The first is the repository's mandatory blanket-offer check. The second
-- proves that a proposed item still points to a service from the same business.
SELECT
  'active_blanket_offer_with_service' AS check_name,
  m.id AS offer_id,
  i.offer_item_id,
  i.service_id,
  m.offer_raw_text
FROM promo_offer_master AS m
JOIN promo_offer_items AS i ON i.offer_id = m.id
WHERE m.is_active
  AND i.service_id IS NOT NULL
  AND m.offer_raw_text ~* '(all services|any service|all products|all treatments|gift card|skin ?care products)'
UNION ALL
SELECT
  'cross_business_service_mapping' AS check_name,
  m.id AS offer_id,
  i.offer_item_id,
  i.service_id,
  m.offer_raw_text
FROM promo_offer_master AS m
JOIN promo_offer_items AS i ON i.offer_id = m.id
JOIN clinic_services AS s ON s.service_id = i.service_id
WHERE i.service_id IS NOT NULL
  AND m.business_id IS DISTINCT FROM s.business_id
ORDER BY check_name, offer_id, offer_item_id;
