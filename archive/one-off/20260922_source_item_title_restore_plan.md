# Source item title restoration plan — 2026-09-22

## Scope

Restore the source-facing `promo_offer_items.item_name` for exactly these 44
confirmed rows. `service_id`, offer activity, prices, quantities, and all
database schema are out of scope.

| Target title | `offer_item_id` |
| --- | --- |
| Neurotoxin | 148, 437, 963, 1025, 1377, 1695, 2642, 2883, 3280, 3283, 3293, 3438, 3439, 4752, 5378 |
| Clear + Brilliant Resurfacing Laser | 968 |
| Versa Filler | 984, 2566 |
| Lip Filler Versa | 2606, 3387 |
| Versa | 3396, 3427 |
| Versa Lip Filler | 5097 |
| Skinvive | 2488 |
| Juvéderm Ultra XC | 2607, 3307 |
| Radiesse | 3286 |
| HIFU | 3357 |
| ezGEL Injections | 3679 |
| Forma Facial | 3689 |
| Lumecca IPL | 3690 |
| VI Peel | 3691, 3721, 3725, 3756 |
| Dermaplaning | 3796 |
| Perfect Derma Peel | 3800 |
| Microchanneling | 3834 |
| Evolysse Filler | 4430, 4542 |
| Xeomin | 5396 |
| Restylane | 5451 |
| Skintyte | 5503, 5504 |

## Frozen dry-run result

At 2026-09-22, all 44 target IDs existed, belonged to their expected offers,
retained the expected overwritten item name, and had source offer text
supporting the restoration. Five rows were concurrently restored to fuller
source phrases before the update reached them; their more exact source-facing
titles are recorded above and were retained. The write condition required the
expected old item name, so any drift was skipped rather than overwritten.

## Apply and acceptance

The apply statement updates only `item_name` and suppresses the existing
triggers for its single transaction, preventing unrelated offer-state or
fingerprint writes. A final readback verified all 44 source titles against
their current offer text. A concurrent mapping process changed some
`service_id` values during this batch; this repair neither updated nor restored
them. The project blanket-offer gate returned zero rows; no offer text was
changed to satisfy it.
