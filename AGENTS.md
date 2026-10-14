# Promotion data quality

When updating `promo_offer_master` or `promo_offer_items`, blanket offers must not be linked to a specific service. Before completing each data-update batch, run the following check. It must return zero rows, unless a row is deliberately retained and documented in the batch result.

```sql
select m.id, m.offer_raw_text, i.item_name
from promo_offer_master m
join promo_offer_items i on i.offer_id = m.id
where m.is_active
  and i.service_id is not null
  and m.offer_raw_text ~* '(all services|any service|all products|all treatments|gift card|skin ?care products)';
```

For any matching row that is not intentionally retained, set `promo_offer_items.service_id` to `NULL`; do not change the offer text solely to make this check pass.

## Monitor synchronization policy

For Firecrawl promotion synchronization, a missing year or unresolved service mapping is not a write blocker. Store unknown campaign dates and service IDs as NULL; retain evidenced item names, prices, quantities and conditions. Preserve a known valid service mapping and existing Offer/Item IDs when the match is unique. Keep `is_offer_extracted=false` while service mapping remains incomplete. Reuse persisted Ego Lite verification for the capture it actually reviewed, keeping its original timestamp; later captures require current visibility evidence. Source/Offer identity ambiguity, contradictory price evidence and manual protections still require review. Never change database schema to implement these rules.
