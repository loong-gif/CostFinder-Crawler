# Offer item delivered-units repair plan

- Generated: 2026-09-22
- Scope: `public.promo_offer_items.delivered_units` only
- Action: set the frozen `delivered_units = 1` values below to `NULL`
- Schema changes: none
- Evidence rule: the source states a price/offer but does not explicitly state a delivered quantity of one syringe, treatment, or session.
- Apply only when `offer_item_id`, `offer_id`, `item_name`, `offer_raw_text`, `delivered_units`, `promo_offer_items.updated_at`, `promo_offer_master.is_active`, `promo_offer_master.updated_at`, and `clinic_promotions.updated_at` still match this snapshot.

| offer_item_id | offer_id | item_name | delivered_units | item_updated_at | offer_is_active | offer_raw_text | offer_updated_at | promotion_id | promotion_updated_at |
| ---: | ---: | --- | ---: | --- | --- | --- | --- | ---: | --- |
| 2373 | 7117 | Dermal Filler | 1 | `2026-09-18 15:40:16.534291+00` | false | `New patients: $100 off filler treatments` | `2026-09-19 02:10:56.356181+00` | 3564 | `2026-09-19 12:23:59.429205+00` |
| 2392 | 8022 | Dermal Filler | 1 | `2026-09-17 13:47:05.298497+00` | true | `Lip Filler $499` | `2026-09-19 01:02:52.441479+00` | 216 | `2026-09-20 15:29:55.894847+00` |
| 2399 | 8177 | Hydrafacial | 1 | `2026-09-15 06:29:27.674455+00` | true | `HydraFacial®: Express Introductory HydraFacial Treatment for $149` | `2026-09-19 01:02:52.441479+00` | 671 | `2026-09-20 15:46:18.150204+00` |
| 2415 | 8522 | Hydrafacial | 1 | `2026-09-15 06:29:27.674455+00` | true | `Signature HydraFacial $149 — new patient only` | `2026-09-19 01:02:52.441479+00` | 216 | `2026-09-20 15:29:55.894847+00` |
| 2436 | 8575 | Laser Hair Removal | 1 | `2026-09-17 13:47:10.378749+00` | false | `Lower Legs (knees included) for only $68 (regular $100).` | `2026-09-21 07:03:04.460735+00` | 3587 | `2026-09-21 07:03:09.42+00` |
| 2437 | 8566 | Laser Hair Removal | 1 | `2026-09-17 13:47:11.046062+00` | false | `Upper Legs (knees not included) for only $75 (regular $85).` | `2026-09-21 07:03:03.561993+00` | 3587 | `2026-09-21 07:03:09.42+00` |
| 2438 | 8560 | Laser Hair Removal | 1 | `2026-09-17 13:47:11.609032+00` | false | `Lower Arms only $42 (regular $50).` | `2026-09-21 07:03:01.892676+00` | 3587 | `2026-09-21 07:03:09.42+00` |

## Apply result

- Applied successfully: 7/7; conflicts or skipped rows: 0.
- Business-field change: all seven `delivered_units` values are now `NULL`.
- Preserved: all seven `service_id`, `item_name`, `offer_raw_text`, offer relationships, and active states.
- Price semantics: all seven offers are `price_model = total`.
- Mandatory blanket-offer gate: 0 rows.
- Schema: no DDL or migration executed.
- Audit timestamps: the item `updated_at` values and the parent offer `updated_at` values were refreshed by the existing update path/trigger.
