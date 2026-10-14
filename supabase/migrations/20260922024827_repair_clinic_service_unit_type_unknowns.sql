BEGIN;

ALTER TABLE public.clinic_services
    ALTER COLUMN unit_type DROP DEFAULT,
    ALTER COLUMN unit_type DROP NOT NULL;

DROP TRIGGER IF EXISTS trg_clinic_services_unit_type_fallback
    ON public.clinic_services;

DROP FUNCTION IF EXISTS public.apply_clinic_service_unit_type_fallback();

-- Reconcile only the values that the September fallback could have invented.
-- A unit is retained only when linked offer text states one unambiguously.
WITH evidence_rows AS (
    SELECT
        cs.service_id,
        CASE
            WHEN lower(coalesce(m.offer_raw_text, '')) ~ '(per[[:space:]]+unit|/[[:space:]]*unit|[0-9]+[[:space:]]+units?|\$[0-9]+(?:\.[0-9]+)?[[:space:]]*units?)' THEN 'unit'
            WHEN lower(coalesce(m.offer_raw_text, '')) ~ '(per[[:space:]]+syringes?|/[[:space:]]*syringes?|[0-9]+[[:space:]]+syringes?|\$[0-9]+(?:\.[0-9]+)?[[:space:]]*syringes?)' THEN 'syringe'
            WHEN lower(coalesce(m.offer_raw_text, '')) ~ '(per[[:space:]]+vials?|/[[:space:]]*vials?|[0-9]+[[:space:]]+vials?|\$[0-9]+(?:\.[0-9]+)?[[:space:]]*vials?)' THEN 'vial'
            WHEN lower(coalesce(m.offer_raw_text, '')) ~ '(per[[:space:]]+sessions?|/[[:space:]]*sessions?|[0-9]+[[:space:]]+sessions?|\$[0-9]+(?:\.[0-9]+)?[[:space:]]*sessions?)' THEN 'session'
            WHEN lower(coalesce(m.offer_raw_text, '')) ~ '(per[[:space:]]+treatments?|/[[:space:]]*treatments?|[0-9]+[[:space:]]+treatments?|\$[0-9]+(?:\.[0-9]+)?[[:space:]]*treatments?)' THEN 'treatment'
            WHEN lower(coalesce(m.offer_raw_text, '')) ~ '(per[[:space:]]+areas?|/[[:space:]]*areas?|[0-9]+[[:space:]]+areas?|\$[0-9]+(?:\.[0-9]+)?[[:space:]]*areas?)' THEN 'area'
            WHEN lower(coalesce(m.offer_raw_text, '')) ~ '(per[[:space:]]+shots?|/[[:space:]]*shots?|[0-9]+[[:space:]]+shots?|\$[0-9]+(?:\.[0-9]+)?[[:space:]]*shots?)' THEN 'shot'
            ELSE NULL
        END AS explicit_unit
    FROM public.clinic_services AS cs
    LEFT JOIN public.promo_offer_items AS i
        ON i.service_id = cs.service_id
    LEFT JOIN public.promo_offer_master AS m
        ON m.id = i.offer_id
    WHERE cs.unit_type IN ('treatment', 'others')
),
evidence AS (
    SELECT
        service_id,
        count(DISTINCT explicit_unit) FILTER (WHERE explicit_unit IS NOT NULL) AS type_count,
        min(explicit_unit) FILTER (WHERE explicit_unit IS NOT NULL) AS explicit_unit
    FROM evidence_rows
    GROUP BY service_id
)
UPDATE public.clinic_services AS cs
SET unit_type = CASE
    WHEN e.type_count = 1 THEN e.explicit_unit
    ELSE NULL
END
FROM evidence AS e
WHERE cs.service_id = e.service_id
  AND cs.unit_type IN ('treatment', 'others');

DO $$
BEGIN
    IF NOT EXISTS (
        SELECT 1
        FROM pg_constraint
        WHERE conrelid = 'public.clinic_services'::regclass
          AND conname = 'clinic_services_unit_type_allowed_check'
    ) THEN
        ALTER TABLE public.clinic_services
            ADD CONSTRAINT clinic_services_unit_type_allowed_check
            CHECK (unit_type IS NULL OR unit_type IN (
                'unit', 'syringe', 'vial', 'session', 'treatment', 'area', 'shot'
            ));
    END IF;
END;
$$;

COMMENT ON COLUMN public.clinic_services.unit_type IS
    'Source-stated billing/delivery unit. NULL means the unit is unknown; do not infer treatment.';

COMMIT;
