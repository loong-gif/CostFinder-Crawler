-- Complete the source-backed price basis for the three active offers still
-- reported by the P0/P1 acceptance audit.
begin;

-- The source gives a per-unit Dysport price and a separate $50 threshold
-- saving, but no comparable regular per-unit price. Keep the explicit sale
-- rate and remove unsupported comparative fields.
update public.promo_offer_master
set regular_price = null,
    discount_amount = null,
    discount_percent = null,
    price_model = 'per_unit',
    updated_at = clock_timestamp()
where id = 7665
  and offer_raw_text = 'Purchase Dysport $4.30 per unit Get $50 off (minimum 120 units)'
  and discount_price = 4.30
  and regular_price is null;

update public.promo_offer_master
set conditions = array[
      '50-unit Xeomin bundle',
      'About 18% less than the equivalent Botox treatment'
    ],
    regular_price = 11,
    discount_price = 9,
    discount_amount = 2,
    discount_percent = 18.18,
    price_model = 'per_unit',
    updated_at = clock_timestamp()
where id = 1415
  and offer_raw_text like 'Our 50-unit Xeomin bundle is $450%'
  and discount_price = 9;

update public.promo_offer_master
set conditions = array[
      'New patients only',
      'Promotional rate applies to the first 40 units',
      'Up to $120 off the first Botox treatment'
    ],
    regular_price = 13,
    discount_price = 10,
    discount_amount = 120,
    discount_percent = 23.08,
    is_new_customer_required = true,
    price_model = 'per_unit',
    updated_at = clock_timestamp()
where id = 8116
  and offer_raw_text = 'Botox at Just $10/Unit'
  and discount_price = 10;

update public.promo_offer_items
set delivered_units = 40,
    unit_price = 10,
    updated_at = clock_timestamp()
where offer_id = 8116
  and service_id = 2240;

commit;
