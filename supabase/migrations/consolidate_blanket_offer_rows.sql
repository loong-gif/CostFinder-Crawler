BEGIN;

-- Blanket discounts belong on city and clinic pages, not treatment pages.
CREATE OR REPLACE FUNCTION public.is_blanket_offer_text(p_offer_raw_text text)
RETURNS boolean
LANGUAGE sql
IMMUTABLE
SET search_path TO public
AS $function$
  SELECT coalesce(p_offer_raw_text, '') ~*
    '(all services|all products|all treatments|any service|gift cards?|skin ?care products|referral program(me)?)';
$function$;

-- Keep source-derived blanket items unlinked even when a later mapper tries
-- to attach one to a catalogue service.
CREATE OR REPLACE FUNCTION public.sync_promo_offer_item_name()
RETURNS trigger
LANGUAGE plpgsql
SET search_path TO public
AS $function$
DECLARE
  v_service_name text;
  v_offer_raw_text text;
BEGIN
  SELECT offer_raw_text
    INTO v_offer_raw_text
    FROM public.promo_offer_master
   WHERE id = NEW.offer_id;

  IF public.is_blanket_offer_text(v_offer_raw_text) THEN
    NEW.service_id := NULL;
    RETURN NEW;
  END IF;

  IF NEW.service_id IS NULL
     OR NULLIF(btrim(NEW.item_name), '') IS NOT NULL THEN
    RETURN NEW;
  END IF;

  SELECT service_name::text
    INTO v_service_name
    FROM public.clinic_services
   WHERE service_id = NEW.service_id;

  IF FOUND THEN
    NEW.item_name := v_service_name;
  END IF;

  RETURN NEW;
END;
$function$;

DROP TRIGGER IF EXISTS trg_sync_promo_offer_item_name ON public.promo_offer_items;
CREATE TRIGGER trg_sync_promo_offer_item_name
BEFORE INSERT OR UPDATE OF offer_id, service_id ON public.promo_offer_items
FOR EACH ROW EXECUTE FUNCTION public.sync_promo_offer_item_name();

CREATE OR REPLACE FUNCTION public.normalize_promo_offer_item_unit_price()
RETURNS trigger
LANGUAGE plpgsql
SET search_path TO public
AS $function$
DECLARE
  v_is_package boolean;
  v_discount_price numeric;
  v_price_model text;
  v_offer_raw_text text;
BEGIN
  SELECT is_package, discount_price, price_model, offer_raw_text
    INTO v_is_package, v_discount_price, v_price_model, v_offer_raw_text
    FROM public.promo_offer_master
   WHERE id = NEW.offer_id
   FOR UPDATE;

  IF NOT FOUND THEN
    RAISE EXCEPTION 'No promo_offer_master found for offer_id=%', NEW.offer_id;
  END IF;

  IF public.is_blanket_offer_text(v_offer_raw_text) OR v_is_package THEN
    NEW.unit_price := NULL;
    RETURN NEW;
  END IF;

  IF v_discount_price IS NULL AND v_price_model = 'per_unit' THEN
    RETURN NEW;
  END IF;

  IF v_discount_price IS NULL THEN
    NEW.unit_price := NULL;
  ELSE
    NEW.unit_price := round(v_discount_price / coalesce(NEW.delivered_units, 1), 4);
  END IF;
  RETURN NEW;
END;
$function$;

CREATE OR REPLACE FUNCTION public.prevent_incomplete_offer_activation()
RETURNS trigger
LANGUAGE plpgsql
SET search_path TO public
AS $function$
BEGIN
  IF NEW.is_active AND (
    (NEW.promotion_id IS NULL) = (NEW.membership_plan_id IS NULL)
    OR (NEW.promotion_id IS NOT NULL AND NOT EXISTS (
      SELECT 1
        FROM public.clinic_promotions promotion
       WHERE promotion.promotion_id = NEW.promotion_id
         AND promotion.business_id = NEW.business_id
         AND promotion.is_active
         AND (promotion.campaign_end_date IS NULL OR promotion.campaign_end_date >= current_date)
    ))
    OR (NEW.membership_plan_id IS NOT NULL AND NOT EXISTS (
      SELECT 1
        FROM public.clinic_memberships membership
       WHERE membership.plan_id = NEW.membership_plan_id
         AND membership.business_id = NEW.business_id
    ))
    OR NOT EXISTS (
      SELECT 1 FROM public.promo_offer_items item WHERE item.offer_id = NEW.id
    )
    OR EXISTS (
      SELECT 1
        FROM public.promo_offer_items item
       WHERE item.offer_id = NEW.id
         AND item.service_id IS NULL
         AND NOT public.is_blanket_offer_text(NEW.offer_raw_text)
    )
    OR (
      NOT public.is_blanket_offer_text(NEW.offer_raw_text)
      AND NEW.is_package = false
      AND NEW.discount_price IS NOT NULL
      AND EXISTS (
        SELECT 1
          FROM public.promo_offer_items item
         WHERE item.offer_id = NEW.id
           AND round(item.unit_price * coalesce(item.delivered_units, 1), 2)
               IS DISTINCT FROM round(NEW.discount_price, 2)
      )
    )
  ) THEN
    NEW.is_active := false;
  END IF;
  RETURN NEW;
END;
$function$;

CREATE OR REPLACE FUNCTION public.sync_non_package_offer_prices(p_offer_id bigint)
RETURNS void
LANGUAGE plpgsql
SECURITY DEFINER
SET search_path TO public
AS $function$
DECLARE
  v_is_package boolean;
  v_discount_price numeric;
  v_price_model text;
  v_offer_raw_text text;
BEGIN
  SELECT is_package, discount_price, price_model, offer_raw_text
    INTO v_is_package, v_discount_price, v_price_model, v_offer_raw_text
    FROM public.promo_offer_master
   WHERE id = p_offer_id
   FOR UPDATE;

  IF NOT FOUND OR v_is_package OR public.is_blanket_offer_text(v_offer_raw_text) THEN
    RETURN;
  END IF;

  IF v_discount_price IS NULL AND v_price_model <> 'per_unit' THEN
    UPDATE public.promo_offer_items
       SET unit_price = NULL
     WHERE offer_id = p_offer_id AND unit_price IS NOT NULL;
  ELSIF v_discount_price IS NOT NULL THEN
    UPDATE public.promo_offer_items item
       SET unit_price = round(v_discount_price / coalesce(item.delivered_units, 1), 4)
     WHERE item.offer_id = p_offer_id
       AND item.unit_price IS DISTINCT FROM round(v_discount_price / coalesce(item.delivered_units, 1), 4);
  END IF;
END;
$function$;

CREATE OR REPLACE FUNCTION public.deactivate_offer_with_unresolved_item()
RETURNS trigger
LANGUAGE plpgsql
SET search_path TO public
AS $function$
BEGIN
  IF EXISTS (
    SELECT 1
      FROM public.promo_offer_master offer
      JOIN public.promo_offer_items item ON item.offer_id = offer.id
     WHERE offer.id = NEW.offer_id
       AND offer.is_active
       AND NOT public.is_blanket_offer_text(offer.offer_raw_text)
       AND offer.is_package = false
       AND offer.discount_price IS NOT NULL
       AND round(NEW.unit_price * coalesce(NEW.delivered_units, 1), 2)
           IS DISTINCT FROM round(offer.discount_price, 2)
  ) THEN
    UPDATE public.promo_offer_master
       SET is_active = false, updated_at = now()
     WHERE id = NEW.offer_id AND is_active;
  END IF;
  RETURN NEW;
END;
$function$;

CREATE OR REPLACE FUNCTION public.validate_non_package_offer_prices()
RETURNS trigger
LANGUAGE plpgsql
SECURITY DEFINER
SET search_path TO public
AS $function$
DECLARE
  v_offer_id bigint;
BEGIN
  IF TG_TABLE_NAME = 'clinic_services' THEN
    RETURN NULL;
  ELSIF TG_TABLE_NAME = 'promo_offer_master' THEN
    v_offer_id := NEW.id;
  ELSIF TG_OP = 'DELETE' THEN
    v_offer_id := OLD.offer_id;
  ELSE
    v_offer_id := NEW.offer_id;
  END IF;

  IF EXISTS (
    SELECT 1
      FROM public.promo_offer_master master
      JOIN public.promo_offer_items item ON item.offer_id = master.id
     WHERE master.id = v_offer_id
       AND NOT public.is_blanket_offer_text(master.offer_raw_text)
       AND master.is_package = false
       AND master.discount_price IS NOT NULL
       AND round(item.unit_price * coalesce(item.delivered_units, 1), 2)
           IS DISTINCT FROM round(master.discount_price, 2)
  ) THEN
    RAISE EXCEPTION 'non-package offer discount invariant violated for offer %', v_offer_id;
  END IF;

  RETURN NULL;
END;
$function$;

CREATE TEMP TABLE _blanket_offer_groups (
  canonical_offer_id bigint PRIMARY KEY,
  offer_ids bigint[] NOT NULL,
  item_name text NOT NULL
) ON COMMIT DROP;

INSERT INTO _blanket_offer_groups (canonical_offer_id, offer_ids, item_name) VALUES
  (10541, ARRAY[10541, 10542, 10543], '15% off all products and services'),
  (10559, ARRAY[10559, 10560, 10561], '20% off all services!'),
  (10562, ARRAY[10562, 10563, 10564, 10565], '15% off all services'),
  (10570, ARRAY[10570, 10571, 10572], 'New-To-You Special — 10% OFF Any Service'),
  (10619, ARRAY[10619, 10620, 10621, 10622, 10623, 10624, 10625, 10626, 10627, 10628], '10% off all services'),
  (10629, ARRAY[10629, 10630, 10631], '50% off all services'),
  (10640, ARRAY[10640, 10641, 10642, 10643, 10644, 10645, 10646, 10647, 10648], '15% off all products and services');

CREATE TEMP TABLE _blanket_offer_plan ON COMMIT DROP AS
SELECT
  group_row.canonical_offer_id,
  offer_id,
  group_row.item_name,
  offer_id <> group_row.canonical_offer_id AS is_duplicate
FROM _blanket_offer_groups group_row
CROSS JOIN LATERAL unnest(group_row.offer_ids) AS expanded(offer_id);

DO $function$
DECLARE
  v_invalid_rows bigint;
BEGIN
  SELECT count(*)
    INTO v_invalid_rows
    FROM _blanket_offer_plan plan
    LEFT JOIN public.promo_offer_master offer ON offer.id = plan.offer_id
   WHERE offer.id IS NULL
      OR NOT public.is_blanket_offer_text(offer.offer_raw_text)
      OR (plan.offer_id = plan.canonical_offer_id AND NOT offer.is_active)
      OR (SELECT count(*) FROM public.promo_offer_items item WHERE item.offer_id = plan.offer_id) <> 1;

  IF v_invalid_rows <> 0 THEN
    RAISE EXCEPTION 'blanket offer consolidation precondition failed for % row(s)', v_invalid_rows;
  END IF;
END;
$function$;

UPDATE public.promo_offer_items item
   SET service_id = NULL,
       item_name = plan.item_name,
       delivered_units = NULL,
       unit_price = NULL,
       updated_at = now()
  FROM _blanket_offer_plan plan
 WHERE item.offer_id = plan.canonical_offer_id
   AND plan.is_duplicate = false;

UPDATE public.promo_offer_master offer
   SET is_active = false,
       updated_at = now()
  FROM _blanket_offer_plan plan
 WHERE offer.id = plan.offer_id
   AND plan.is_duplicate
   AND offer.is_active;

DO $function$
DECLARE
  v_invalid_rows bigint;
  v_linked_blankets bigint;
BEGIN
  SELECT count(*)
    INTO v_invalid_rows
    FROM _blanket_offer_plan plan
    JOIN public.promo_offer_master offer ON offer.id = plan.offer_id
    JOIN public.promo_offer_items item ON item.offer_id = offer.id
   WHERE (plan.is_duplicate AND offer.is_active)
      OR (NOT plan.is_duplicate AND (
        NOT offer.is_active
        OR item.service_id IS NOT NULL
        OR item.item_name IS DISTINCT FROM plan.item_name
        OR item.delivered_units IS NOT NULL
        OR item.unit_price IS NOT NULL
      ));

  SELECT count(*)
    INTO v_linked_blankets
    FROM public.promo_offer_master offer
    JOIN public.promo_offer_items item ON item.offer_id = offer.id
   WHERE offer.is_active
     AND item.service_id IS NOT NULL
     AND public.is_blanket_offer_text(offer.offer_raw_text);

  IF v_invalid_rows <> 0 OR v_linked_blankets <> 0 THEN
    RAISE EXCEPTION
      'blanket offer consolidation validation failed: % target row(s), % linked active blanket item(s)',
      v_invalid_rows, v_linked_blankets;
  END IF;
END;
$function$;

COMMIT;
