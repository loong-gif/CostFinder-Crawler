BEGIN;

-- NULL remains meaningful for per-unit, package, and variable-count offers.
-- Store that meaning alongside the item instead of making the count look like
-- an extraction omission. Existing quantity-specific reasons are preserved.
UPDATE public.promo_offer_items AS item
   SET reason = concat_ws(
           ' ',
           nullif(btrim(item.reason), ''),
           CASE
               WHEN offer.price_model = 'per_unit' THEN
                   'Per-unit price; quantity intentionally NULL.'
               WHEN lower(coalesce(offer.offer_raw_text, '')) ~
                    '(unlimited|variable|package|bundle|series|membership|minimum|at least|up to|choose|choice|per[[:space:]]+(vial|syringe|treatment|area|session)|each[[:space:]]+(unit|vial|syringe|treatment|area|session)|[0-9]+[[:space:]]*(units?|syringes?|treatments?|sessions?|vials?))' THEN
                   'Source expresses a package, eligibility, or variable count; quantity intentionally NULL.'
               ELSE
                   'No source-backed item count; quantity intentionally NULL pending evidence.'
           END
       ),
       updated_at = now()
  FROM public.promo_offer_master AS offer
 WHERE item.offer_id = offer.id
   AND offer.is_active
   AND item.quantity IS NULL
   AND coalesce(lower(item.reason), '') NOT LIKE '%quantity intentionally null%';

COMMIT;
