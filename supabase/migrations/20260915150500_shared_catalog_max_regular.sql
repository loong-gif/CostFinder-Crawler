-- Shared catalog conflict: use the highest implied unit as 原价.
-- Multi-service rows whose implied unit > 100 are package totals, not catalog units.

WITH n_svcs AS (
  SELECT offer_id, count(DISTINCT service_id) AS n_svcs
  FROM public.promo_offer_items
  GROUP BY offer_id
),
voters AS (
  SELECT
    i.offer_id,
    i.service_id,
    i.quantity,
    m.regular_price,
    m.discount_price,
    CASE
      WHEN i.quantity IS NULL OR i.quantity = 1 THEN m.regular_price
      ELSE round(m.regular_price / i.quantity, 2)
    END AS implied_unit
  FROM public.promo_offer_items i
  JOIN public.promo_offer_master m ON m.id = i.offer_id
  JOIN n_svcs n ON n.offer_id = i.offer_id
  WHERE m.is_active
    AND m.regular_price IS NOT NULL
    AND i.service_id IS NOT NULL
    AND NOT (
      n.n_svcs > 1
      AND (
        CASE
          WHEN i.quantity IS NULL OR i.quantity = 1 THEN m.regular_price
          ELSE round(m.regular_price / i.quantity, 2)
        END
      ) > 100
    )
),
agg AS (
  SELECT service_id, max(implied_unit) AS max_unit
  FROM voters
  GROUP BY service_id
  HAVING count(DISTINCT offer_id) > 1
),
upd_svc AS (
  UPDATE public.clinic_services s
  SET regular_price = a.max_unit
  FROM agg a
  WHERE s.service_id = a.service_id
    AND (s.regular_price IS NULL OR s.regular_price < a.max_unit)
  RETURNING s.service_id
),
bump AS (
  SELECT
    v.offer_id,
    max(round(a.max_unit * coalesce(v.quantity, 1), 2)) AS new_reg
  FROM voters v
  JOIN agg a ON a.service_id = v.service_id
  GROUP BY v.offer_id
)
UPDATE public.promo_offer_master m
SET regular_price = b.new_reg
FROM bump b
WHERE m.id = b.offer_id
  AND b.new_reg > m.regular_price
  AND (m.discount_price IS NULL OR m.discount_price <= b.new_reg);

-- Offer 3735 has a third service that only appears on the cheaper row.
UPDATE public.clinic_services
SET regular_price = 51.50
WHERE service_id = 343
  AND (regular_price IS NULL OR regular_price < 51.50);
