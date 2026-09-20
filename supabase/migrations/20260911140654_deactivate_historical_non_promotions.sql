BEGIN;

-- These source records were verified as historical campaigns, regular service
-- pages, training/event pages, or expired specials. They have no active offers,
-- so keeping the parent promotion active only misstates current availability.
UPDATE public.clinic_promotions
   SET is_active = false,
       updated_at = now()
 WHERE source_url IN (
     'https://greenspringaesthetics.com/october-specials',
     'https://vivadayspa.com/news-tips/21st-anniversary-specials',
     'https://medaestheticsoc.com/special',
     'https://smileyaesthetics.com/events/special-training-sculptra-pdgf-nashville-06-20-2026',
     'https://glowupmedspa.com/products/lip-filler',
     'https://www.rmaok.com/specials'
   )
   AND is_active;

COMMIT;
