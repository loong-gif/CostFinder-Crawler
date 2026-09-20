-- Membership offers are distinct by membership plan. Align the database
-- fingerprint source key with the application fingerprint contract.
CREATE OR REPLACE FUNCTION public.fn_offer_source_key(
    p_membership_plan_id bigint,
    p_promotion_id bigint,
    p_business_id bigint
)
RETURNS text
LANGUAGE sql
IMMUTABLE
SET search_path = public
AS $function$
    SELECT CASE
        WHEN p_membership_plan_id IS NOT NULL THEN 'm:' || p_membership_plan_id::text
        WHEN p_promotion_id IS NOT NULL THEN 'p:' || p_promotion_id::text
        ELSE 'b:' || coalesce(p_business_id::text, '')
    END;
$function$;
