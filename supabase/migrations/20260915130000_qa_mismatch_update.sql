-- QA mismatch upstream + offer sync (production kdlpkjzcnbkjcvwsvlwn)
-- Scope: 48 offer_ids from QA notes (2026-09-15)
-- Rules: no DELETE on promo_offer_master.id; offer_fingerprint=NULL on text/price changes

BEGIN;

-- ---------------------------------------------------------------------------
-- 28 Belle Vie — seasonal $8.99 special no longer on page
-- ---------------------------------------------------------------------------
UPDATE public.clinic_promotions
SET
  promotion_title = 'Special Offers | Belle Vie',
  promotion_content = ARRAY[
    'Explore treatment bundles and series for skin, facial definition, body and wellness',
    'Current pricing and availability live in your location webstore',
    'Previous seasonal pricing (including wrinkle relaxer $8.99/unit special) is no longer displayed',
    'Membership banks 100% of dollars for treatments with best pricing all year'
  ]::text[]),
  is_offer_extracted = false,
  is_active = true,
  updated_at = now()
WHERE promotion_id = 28;

UPDATE public.promo_offer_master
SET is_active = false, offer_fingerprint = NULL, updated_at = now()
WHERE id = 7728;

-- ---------------------------------------------------------------------------
-- 105 Enve — JUVÉDERM DAY gift card (replace stale Botox anniversary reward)
-- ---------------------------------------------------------------------------
UPDATE public.promo_offer_master
SET
  offer_raw_text = 'JUVÉDERM DAY 2026: Buy a $75 gift card, get a second card free; redeem within 30 days for an extra $75 off your treatment at Enve',
  regular_price = NULL,
  discount_price = NULL,
  discount_percent = NULL,
  discount_amount = 75,
  price_model = 'total',
  is_membership_required = false,
  is_new_customer_required = false,
  is_active = true,
  offer_fingerprint = NULL,
  updated_at = now()
WHERE id = 1698 AND promotion_id = 105;

-- ---------------------------------------------------------------------------
-- 107 YoungerLook — keep 26% headline aligned with live page
-- ---------------------------------------------------------------------------
UPDATE public.promo_offer_master
SET
  offer_raw_text = 'Save 26% on Botox Injections, Xeomin & Dysport in Orange County (membership program pricing)',
  discount_percent = 26,
  is_membership_required = true,
  is_active = true,
  offer_fingerprint = NULL,
  updated_at = now()
WHERE id = 1699 AND promotion_id = 107;

-- ---------------------------------------------------------------------------
-- 109 Topanga — refresh specials; reactivate XERF list prices; fix filler tier
-- ---------------------------------------------------------------------------
UPDATE public.clinic_promotions
SET
  promotion_title = 'Specials | Topanga Beauty Center',
  promotion_content = ARRAY[
    'XERF launch: save up to 15% on skin tightening packages',
    'XERF Full Face $1,950 · XERF Face + Neck $2,350 · XERF Body Area $1,750',
    'Dermal Filler $650 per syringe; any two syringes 20% off',
    '40-Unit Tox Special — Botox, Xeomin, or Jeuveau for $365',
    'Laser Hair Removal 20% off for first-time patients',
    'PRP facial with micro-needling list price $650 (no current $399 special on specials page)'
  ]::text[]),
  is_offer_extracted = true,
  is_active = true,
  updated_at = now()
WHERE promotion_id = 109;

UPDATE public.promo_offer_master
SET
  offer_raw_text = 'Dermal Filler: $650 per syringe; buy 2 syringes and save 20%',
  discount_percent = 20,
  regular_price = NULL,
  discount_price = NULL,
  discount_amount = NULL,
  is_active = true,
  offer_fingerprint = NULL,
  updated_at = now()
WHERE id = 9199 AND promotion_id = 109;

UPDATE public.promo_offer_master
SET is_active = false, offer_fingerprint = NULL, updated_at = now()
WHERE id = 9203 AND promotion_id = 109;

UPDATE public.promo_offer_master
SET
  offer_raw_text = 'XERF Full Face Skin Tightening: $1,950',
  regular_price = NULL,
  discount_price = 1950,
  discount_percent = NULL,
  discount_amount = NULL,
  price_model = 'total',
  is_active = true,
  offer_fingerprint = NULL,
  updated_at = now()
WHERE id = 9205 AND promotion_id = 109;

UPDATE public.promo_offer_master
SET
  offer_raw_text = 'XERF Face + Neck Skin Tightening: $2,350',
  regular_price = NULL,
  discount_price = 2350,
  discount_percent = NULL,
  discount_amount = NULL,
  price_model = 'total',
  is_active = true,
  offer_fingerprint = NULL,
  updated_at = now()
WHERE id = 9206 AND promotion_id = 109;

UPDATE public.promo_offer_master
SET
  offer_raw_text = 'XERF Body Area Skin Tightening (abdomen, arms, or thighs): $1,750',
  regular_price = NULL,
  discount_price = 1750,
  discount_percent = NULL,
  discount_amount = NULL,
  price_model = 'total',
  is_active = true,
  offer_fingerprint = NULL,
  updated_at = now()
WHERE id = 9207 AND promotion_id = 109;

UPDATE public.promo_offer_items
SET unit_price = 1950, updated_at = now()
WHERE offer_id = 9205;

UPDATE public.promo_offer_items
SET unit_price = 2350, updated_at = now()
WHERE offer_id = 9206;

UPDATE public.promo_offer_items
SET unit_price = 1750, updated_at = now()
WHERE offer_id = 9207;

-- ---------------------------------------------------------------------------
-- 138 La Queen — retire Year-End 75% rows; add New Guest 50% special
-- ---------------------------------------------------------------------------
UPDATE public.promo_offer_master
SET is_active = false, offer_fingerprint = NULL, updated_at = now()
WHERE id IN (1710, 7644) AND promotion_id = 138;

INSERT INTO public.promo_offer_master (
  business_id, promotion_id, offer_raw_text, discount_percent, price_model,
  is_membership_required, is_new_customer_required, is_active
)
SELECT
  456, 138, 'New Guest Special 50% OFF Lip Filler • Tox • Glow Treatments', 50, 'total',
  false, true, true
WHERE NOT EXISTS (
  SELECT 1 FROM public.promo_offer_master
  WHERE promotion_id = 138
    AND is_active
    AND offer_raw_text ILIKE 'New Guest Special 50% OFF%'
);

-- ---------------------------------------------------------------------------
-- 149 Skinjectables — Sep Birthday Month in-place on existing ids
-- ---------------------------------------------------------------------------
UPDATE public.clinic_promotions
SET
  promotion_title = 'September Birthday Month Specials | Skinjectables',
  promotion_content = ARRAY[
    'September Specials — Limited-Time Savings (Sep 1–30)',
    'Vault Members: Botox $9.50/unit · Jeuveau $9/unit · Daxxify $6.50/unit · Dysport $5/unit',
    'Non Members: Botox $10/unit · Jeuveau $9.50/unit · Daxxify $6.50/unit · Dysport $5.50/unit',
    'New Dysport Clients: 50+ units = 2 FREE treatment areas (25 units each)',
    'Juvederm Filler: Buy One Get One Free',
    'Sculptra: Buy 2 vials get 3rd 50% off; Buy 3 vials get 4th FREE'
  ]::text[]),
  is_ocr_required = true,
  is_offer_extracted = true,
  is_active = true,
  updated_at = now()
WHERE promotion_id = 149;

UPDATE public.promo_offer_master
SET is_active = false, offer_fingerprint = NULL, updated_at = now()
WHERE id = 7737 AND promotion_id = 149;

UPDATE public.promo_offer_master
SET
  offer_raw_text = 'Vault Members Botox $9.50/unit (Birthday Month Sep 1-30)',
  discount_price = 9.50,
  regular_price = NULL,
  discount_percent = NULL,
  discount_amount = NULL,
  price_model = 'per_unit',
  is_membership_required = true,
  is_new_customer_required = false,
  is_active = true,
  offer_fingerprint = NULL,
  updated_at = now()
WHERE id = 7662 AND promotion_id = 149;

UPDATE public.promo_offer_master
SET
  offer_raw_text = 'Vault Members Jeuveau $9/unit (Birthday Month Sep 1-30)',
  discount_price = 9.00,
  regular_price = NULL,
  discount_percent = NULL,
  discount_amount = NULL,
  price_model = 'per_unit',
  is_membership_required = true,
  is_new_customer_required = false,
  is_active = true,
  offer_fingerprint = NULL,
  updated_at = now()
WHERE id = 4373 AND promotion_id = 149;

UPDATE public.promo_offer_master
SET
  offer_raw_text = 'Non Members Jeuveau $9.50/unit (Birthday Month Sep 1-30)',
  discount_price = 9.50,
  regular_price = NULL,
  discount_percent = NULL,
  discount_amount = NULL,
  price_model = 'per_unit',
  is_membership_required = false,
  is_new_customer_required = false,
  is_active = true,
  offer_fingerprint = NULL,
  updated_at = now()
WHERE id = 7643 AND promotion_id = 149;

UPDATE public.promo_offer_items SET unit_price = 9.50, updated_at = now() WHERE offer_id = 7662;
UPDATE public.promo_offer_items SET unit_price = 9.00, updated_at = now() WHERE offer_id = 4373;
UPDATE public.promo_offer_items SET unit_price = 9.50, updated_at = now() WHERE offer_id = 7643;

INSERT INTO public.promo_offer_master (
  business_id, promotion_id, offer_raw_text, discount_price, price_model,
  is_membership_required, is_new_customer_required, is_active
)
SELECT 346, 149, 'Non Members Botox $10/unit (Birthday Month Sep 1-30)', 10.00, 'per_unit', false, false, true
WHERE NOT EXISTS (
  SELECT 1 FROM public.promo_offer_master
  WHERE promotion_id = 149 AND is_active AND offer_raw_text ILIKE 'Non Members Botox $10%'
);

INSERT INTO public.promo_offer_master (
  business_id, promotion_id, offer_raw_text, discount_price, price_model,
  is_membership_required, is_new_customer_required, is_active
)
SELECT 346, 149, 'Vault Members Dysport $5/unit (Birthday Month Sep 1-30)', 5.00, 'per_unit', true, false, true
WHERE NOT EXISTS (
  SELECT 1 FROM public.promo_offer_master
  WHERE promotion_id = 149 AND is_active AND offer_raw_text ILIKE 'Vault Members Dysport $5%'
);

INSERT INTO public.promo_offer_master (
  business_id, promotion_id, offer_raw_text, discount_price, price_model,
  is_membership_required, is_new_customer_required, is_active
)
SELECT 346, 149, 'Non Members Dysport $5.50/unit (Birthday Month Sep 1-30)', 5.50, 'per_unit', false, false, true
WHERE NOT EXISTS (
  SELECT 1 FROM public.promo_offer_master
  WHERE promotion_id = 149 AND is_active AND offer_raw_text ILIKE 'Non Members Dysport $5.50%'
);

-- ---------------------------------------------------------------------------
-- 207 Beauty Fix — form-only page; keep offer inactive
-- ---------------------------------------------------------------------------
UPDATE public.clinic_promotions
SET
  promotion_content = ARRAY[
    'Monthly Specials page shows lead capture / newsletter form',
    'No priced 10% Off Any Treatment terms currently displayed on live page'
  ]::text[]),
  is_offer_extracted = false,
  updated_at = now()
WHERE promotion_id = 207;

-- 9804 already inactive

-- ---------------------------------------------------------------------------
-- 209 Omaha PMC — reactivate simplified new-patient Botox special
-- ---------------------------------------------------------------------------
UPDATE public.promo_offer_master
SET
  offer_raw_text = 'New Patient BOTOX Appointment: 20 units for $179 (regular $300)',
  regular_price = 300,
  discount_price = 179,
  discount_amount = 121,
  discount_percent = 40.33,
  price_model = 'total',
  is_new_customer_required = true,
  is_active = true,
  offer_fingerprint = NULL,
  updated_at = now()
WHERE id = 8122 AND promotion_id = 209;

UPDATE public.promo_offer_items
SET quantity = 20, unit_price = NULL, updated_at = now()
WHERE offer_id = 8122;

-- ---------------------------------------------------------------------------
-- 848 PCH — already note-only upstream; ensure offers stay inactive
-- ---------------------------------------------------------------------------
UPDATE public.clinic_promotions
SET is_offer_extracted = false, updated_at = now()
WHERE promotion_id = 848;

UPDATE public.promo_offer_master
SET is_active = false, updated_at = now()
WHERE promotion_id = 848 AND is_active IS DISTINCT FROM false;

-- ---------------------------------------------------------------------------
-- 928 Facial Aesthetics — $100 off first treatment
-- ---------------------------------------------------------------------------
UPDATE public.clinic_promotions
SET
  promotion_title = 'Offers | Facial Aesthetics',
  promotion_content = ARRAY[
    '$100 OFF YOUR FIRST TREATMENT',
    'New client introductory offer'
  ]::text[]),
  is_offer_extracted = true,
  updated_at = now()
WHERE promotion_id = 928;

UPDATE public.promo_offer_master
SET
  offer_raw_text = '$100 OFF YOUR FIRST TREATMENT',
  regular_price = NULL,
  discount_price = NULL,
  discount_percent = NULL,
  discount_amount = 100,
  price_model = 'total',
  is_new_customer_required = true,
  is_active = true,
  offer_fingerprint = NULL,
  updated_at = now()
WHERE id = 8224 AND promotion_id = 928;

-- ---------------------------------------------------------------------------
-- 3416 DermFX — September specials replace August Restylane-L pricing
-- ---------------------------------------------------------------------------
UPDATE public.clinic_promotions
SET
  promotion_title = 'September Specials | DermFX',
  promotion_content = ARRAY[
    'September Specials',
    'Dysport $50 off',
    'Filler dealer''s choice $75 off',
    '50% Off Laser Hair Removal (Extra Small $27.50 · Small $47.50 · Medium $82.50 · Large $175 · Extra Large $250)'
  ]::text[]),
  is_offer_extracted = true,
  updated_at = now()
WHERE promotion_id = 3416;

UPDATE public.promo_offer_master
SET is_active = false, offer_fingerprint = NULL, updated_at = now()
WHERE id IN (8056, 8057, 8492) AND promotion_id = 3416;

INSERT INTO public.promo_offer_master (
  business_id, promotion_id, offer_raw_text, discount_amount, price_model,
  is_membership_required, is_new_customer_required, is_active
)
SELECT 3310, 3416, 'Dysport $50 off (September Specials)', 50, 'total', false, false, true
WHERE NOT EXISTS (
  SELECT 1 FROM public.promo_offer_master
  WHERE promotion_id = 3416 AND is_active AND offer_raw_text ILIKE 'Dysport $50 off%'
);

INSERT INTO public.promo_offer_master (
  business_id, promotion_id, offer_raw_text, discount_amount, price_model,
  is_membership_required, is_new_customer_required, is_active
)
SELECT 3310, 3416, 'Filler dealer''s choice $75 off (September Specials)', 75, 'total', false, false, true
WHERE NOT EXISTS (
  SELECT 1 FROM public.promo_offer_master
  WHERE promotion_id = 3416 AND is_active AND offer_raw_text ILIKE 'Filler dealer%$75 off%'
);

-- ---------------------------------------------------------------------------
-- 3495 Mariposa — remove stale neurotoxin 20% offer
-- ---------------------------------------------------------------------------
UPDATE public.clinic_promotions
SET
  promotion_content = ARRAY[
    'Save $150 on SKINVIVE Treatment',
    'Special offer for ProGen PRP injectable or topical treatments: buy 3, get 1 free',
    'Gifts With Purchase on select skincare products'
  ]::text[]),
  is_offer_extracted = true,
  updated_at = now()
WHERE promotion_id = 3495;

UPDATE public.promo_offer_master
SET is_active = false, offer_fingerprint = NULL, updated_at = now()
WHERE id = 8540 AND promotion_id = 3495;

-- ---------------------------------------------------------------------------
-- 3548 Island Dermatology — QA could not match $65 promo on page
-- ---------------------------------------------------------------------------
UPDATE public.clinic_promotions
SET
  promotion_content = ARRAY[
    'Specials page lists New to Botox $65 off for eligible Allē members',
    'Live page did not expose a directly verifiable priced offer block during QA (2026-09-15)'
  ]::text[]),
  is_offer_extracted = false,
  updated_at = now()
WHERE promotion_id = 3548;

UPDATE public.promo_offer_master
SET is_active = false, offer_fingerprint = NULL, updated_at = now()
WHERE id = 8005 AND promotion_id = 3548;

-- ---------------------------------------------------------------------------
-- 3577 Vasu — Jeuveau/Letybo 10% replaces Allergan $20
-- ---------------------------------------------------------------------------
UPDATE public.clinic_promotions
SET
  promotion_title = 'Monthly Skincare Specials | Vasu Skin Solutions',
  promotion_content = ARRAY[
    'Purchase 100 units of Jeuveau or Letybo, 10% off',
    'Laser Hair Removal Specials and monthly skincare promotions'
  ]::text[]),
  is_offer_extracted = true,
  updated_at = now()
WHERE promotion_id = 3577;

UPDATE public.promo_offer_master
SET is_active = false, offer_fingerprint = NULL, updated_at = now()
WHERE id = 6939 AND promotion_id = 3577;

INSERT INTO public.promo_offer_master (
  business_id, promotion_id, offer_raw_text, discount_percent, price_model,
  is_membership_required, is_new_customer_required, is_active
)
SELECT 1194, 3577, 'Purchase 100 units of Jeuveau or Letybo, 10% off', 10, 'total', false, false, true
WHERE NOT EXISTS (
  SELECT 1 FROM public.promo_offer_master
  WHERE promotion_id = 3577 AND is_active AND offer_raw_text ILIKE 'Purchase 100 units%'
);

-- ---------------------------------------------------------------------------
-- 3587 New Look — reactivate Nail Fungus 10% (QA: still on page)
-- ---------------------------------------------------------------------------
UPDATE public.promo_offer_master
SET
  offer_raw_text = '10% OFF Nail Fungus Treatment',
  discount_percent = 10,
  is_active = true,
  offer_fingerprint = NULL,
  updated_at = now()
WHERE id = 8591 AND promotion_id = 3587;

-- ---------------------------------------------------------------------------
-- 3590 Fix Med Spa — September laser + PicoWay (retire body PicoWay $200/$400 rows)
-- ---------------------------------------------------------------------------
UPDATE public.clinic_promotions
SET
  promotion_title = 'Specials & Promotions | Fix Med Spa',
  promotion_content = ARRAY[
    'September Specials — Save on laser hair removal and tattoo removal',
    'Laser hair removal: full Brazilian and underarms $55, full legs $115, full face and neck $45',
    'Purchase PicoWay skin tightening (face) or microneedling with PRP (face) and get the neck treatment free',
    'Buy 3 tattoo removal sessions and get 15% off',
    'Discounts cannot be combined with any other offers or promotions'
  ]::text[]),
  is_offer_extracted = false,
  updated_at = now()
WHERE promotion_id = 3590;

UPDATE public.promo_offer_master
SET is_active = false, offer_fingerprint = NULL, updated_at = now()
WHERE id IN (8569, 8584, 8598) AND promotion_id = 3590;

-- ---------------------------------------------------------------------------
-- 3538 Petra — upstream already HIFU 40%; offers remain inactive
-- 3540 Skin Appeal — upstream already Annual Beauty Affair; offers remain inactive
-- 144 NuBella / 865 JTN — already 404 upstream; offers remain inactive
-- 3588 Skinjectables duplicate promo — offers remain inactive
-- ---------------------------------------------------------------------------

-- ---------------------------------------------------------------------------
-- Restorative memberships 342–345 — Beauty Hive replacement (from live retry)
-- ---------------------------------------------------------------------------
UPDATE public.clinic_memberships
SET
  membership_name = 'Essential',
  membership_price = 175.00,
  billing_period = 'monthly',
  minimum_commitment_months = 3,
  source_url = 'https://restorativeinjectables.com/the-beauty-hive',
  benefits = '[
    "Choose one monthly treatment: Dermaplane Facial",
    "Go + Glow",
    "Cold Plasma",
    "Microderm",
    "Custom Medical Facial",
    "3 months minimum",
    "Skip a month twice per year (after 3 months)"
  ]'::jsonb,
  is_offer_extracted = false,
  updated_at = now()
WHERE plan_id = 342;

UPDATE public.clinic_memberships
SET
  membership_name = 'Essential Annual',
  membership_price = 1925.00,
  billing_period = 'annual',
  minimum_commitment_months = 12,
  source_url = 'https://restorativeinjectables.com/the-beauty-hive',
  benefits = '[
    "Essential tier annual billing — one month free ($1,925)",
    "Choose one monthly treatment: Dermaplane Facial",
    "Go + Glow",
    "Cold Plasma",
    "Microderm",
    "Custom Medical Facial"
  ]'::jsonb,
  is_offer_extracted = false,
  updated_at = now()
WHERE plan_id = 343;

UPDATE public.clinic_memberships
SET
  membership_name = 'Signature',
  membership_price = 275.00,
  billing_period = 'monthly',
  minimum_commitment_months = 3,
  source_url = 'https://restorativeinjectables.com/the-beauty-hive',
  benefits = '[
    "Choose one monthly treatment: The Perfect Derma Peel",
    "Skinwave",
    "Target Cool Infusion Facial",
    "Microneedling",
    "60 min Bespoke Facial",
    "IPL",
    "LHR Lip, Chin & Underarm",
    "3 months minimum"
  ]'::jsonb,
  is_offer_extracted = false,
  updated_at = now()
WHERE plan_id = 344;

UPDATE public.clinic_memberships
SET
  membership_name = 'Signature Annual',
  membership_price = 3025.00,
  billing_period = 'annual',
  minimum_commitment_months = 12,
  source_url = 'https://restorativeinjectables.com/the-beauty-hive',
  benefits = '[
    "Signature tier annual billing — one month free ($3,025)",
    "Choose one monthly treatment: The Perfect Derma Peel",
    "Skinwave",
    "Target Cool Infusion Facial",
    "Microneedling",
    "60 min Bespoke Facial",
    "IPL",
    "LHR Lip, Chin & Underarm"
  ]'::jsonb,
  is_offer_extracted = false,
  updated_at = now()
WHERE plan_id = 345;

UPDATE public.promo_offer_master
SET is_active = false, offer_fingerprint = NULL, updated_at = now()
WHERE membership_plan_id IN (342, 343, 344, 345) AND is_active IS DISTINCT FROM false;

-- ---------------------------------------------------------------------------
-- MedSpot memberships 461/462 — image-only Fresh Face Club; no priced offers
-- ---------------------------------------------------------------------------
UPDATE public.clinic_memberships
SET
  source_url = 'https://www.themedspotla.com/membership/',
  benefits = '[
    "September 2026 Fresh Face Club packages shown in membership image",
    "Packages include Skin Restore, Microneedling Bootcamp, Pigment Focused, and Texture Focused tiers",
    "Specific package pricing is image-only on the current source page"
  ]'::jsonb,
  is_offer_extracted = false,
  updated_at = now()
WHERE plan_id IN (461, 462);

COMMIT;
