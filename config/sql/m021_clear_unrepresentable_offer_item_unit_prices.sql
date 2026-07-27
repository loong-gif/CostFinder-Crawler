BEGIN;

-- A whole-offer total that cannot be expressed as a four-decimal per-unit
-- amount must not retain a mathematically contradictory unit_price.
WITH logical_offers AS (
    SELECT
        item.offer_id,
        sum(item.quantity)::integer AS total_quantity
    FROM public.promo_offer_items AS item
    GROUP BY item.offer_id
    HAVING count(*) = 1
       OR (
            count(*) > 1
            AND count(*) FILTER (WHERE item.service_id IS NULL) = 0
            AND count(DISTINCT item.service_id) = 1
       )
), unrepresentable_offers AS (
    SELECT logical_offer.offer_id
    FROM logical_offers AS logical_offer
    JOIN public.promo_offer_master AS master ON master.id = logical_offer.offer_id
    WHERE master.discount_price IS NOT NULL
      AND logical_offer.total_quantity > 0
      AND round(master.discount_price / logical_offer.total_quantity, 4)
          * logical_offer.total_quantity <> master.discount_price
)
UPDATE public.promo_offer_items AS item
SET unit_price = NULL
FROM unrepresentable_offers
WHERE item.offer_id = unrepresentable_offers.offer_id
  AND item.unit_price IS NOT NULL;

COMMIT;
