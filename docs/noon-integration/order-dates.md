# Noon order dates

## `items[].estimated_shipping_date`

- Not documented in Noon's API docs (`noon_api_full.md`/`noon_api_full.yml`). Observed on prod orders from 2026-10-29 to 2026-11-04.
- Format: `"YYYY-MM-DD HH:MM:SS"`.
- Observed time is always 09:00 or 10:00. This is a fixed Cairo noon expressed in UTC: 09:00 when Cairo was UTC+3, 10:00 at UTC+2. So the value is read as a UTC value whose date part equals the Cairo date. The code takes the date part only (`integrations/noon/fulfillment.py` `parse_estimated_shipping_date`).
- Use: it is each Sales Order item's delivery date (earliest value per `partner_sku` among eligible units). The date is clamped to the received date (ERPNext rejects a delivery date before the order date). With no usable value, the item uses the received date.
- A linked (already synced) order keeps the dates stored in its last synced snapshot, so this field never makes a synced order look modified.

## `order_created_at`

Also UTC. `Marketplace Order.received_on` is this value plus 2 hours or 3 hours (Cairo offset).

## Header date

The Sales Order header `delivery_date` is set to the earliest item date by the sync, but ERPNext's own `validate_delivery_date` later sets it to the latest item date.
