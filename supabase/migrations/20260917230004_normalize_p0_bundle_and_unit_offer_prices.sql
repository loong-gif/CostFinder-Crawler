-- Align the remaining source-backed offers with the live normalized price
-- contract: discount_price is the whole-offer amount when a quantity is
-- delivered, while per-unit offers do not store a threshold as delivered.
begin;

-- The source explicitly states a 50-unit bundle for $450 ($9/unit), about
-- 18% below the comparable $550 total. Store the total on the master and the
-- stated unit amount on the item.
update public.promo_offer_master
set regular_price = 550,
    discount_price = 450,
    discount_amount = 100,
    discount_percent = 18.18,
    price_model = 'total',
    updated_at = clock_timestamp()
where id = 1415
  and offer_raw_text like 'Our 50-unit Xeomin bundle is $450%';

update public.promo_offer_items
set delivered_units = 50,
    unit_price = 9,
    updated_at = clock_timestamp()
where offer_id = 1415
  and service_id = 2341;

-- The first-40-units statement is a redemption cap, not a delivered bundle.
-- Keep the per-unit price basis and express the $120 maximum in conditions.
update public.promo_offer_master
set regular_price = 13,
    discount_price = 10,
    discount_amount = 3,
    discount_percent = 23.08,
    conditions = array[
      'New patients only',
      'Promotional rate applies to the first 40 units',
      'Up to $120 off the first Botox treatment'
    ],
    is_new_customer_required = true,
    price_model = 'per_unit',
    updated_at = clock_timestamp()
where id = 8116
  and offer_raw_text = 'Botox at Just $10/Unit';

update public.promo_offer_items
set delivered_units = null,
    unit_price = 10,
    updated_at = clock_timestamp()
where offer_id = 8116
  and service_id = 2240;

commit;
