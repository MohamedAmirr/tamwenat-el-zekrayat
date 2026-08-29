# Talabat integration

Source spec: `openapi.json` (OpenAPI 3.0.3, "Talabat Partner API" v2.0.2), copied from `~/Desktop/talabat-api.json`. Re-copy it here if Talabat publishes an updated spec — don't hand-edit this file.

This will back the `talabat_integration` custom Frappe app (not scaffolded yet — see `CLAUDE.md` "Custom app plan").

## What the API covers

Base path `/v2`, sandbox at `https://sandbox.partner.deliveryhero.io` (separate credentials from production; same paths/payloads). Key endpoints:

- `POST /oauth/token` — auth
- `.../chains/{chain_id}/catalog` (+ `/jobs/{job_id}`) — product catalog upload/sync, async via job polling
- `.../vendors/{vendor_id}/catalog`, `.../vendors/{vendor_id}/catalog/export` — per-vendor catalog
- `.../vendors/{vendor_id}/categories` — category management
- `.../orders/{order_id}`, `.../vendors/{vendor_id}/orders` — order retrieval/status
- `.../promotion` (+ `/jobs/{job_id}`) — promotions
- `.../vendors/{vendor_id}/status` — vendor online/offline status
- `x-webhooks` in the spec — real-time order/job-completion notifications (need a public endpoint on our side to receive these)

Glossary: **Chain** = brand/group of stores, one API key covers all its **Vendors** (individual store outlets). Long-running actions (catalog import, promotions) run as async **Jobs**, polled by ID.

## Open questions before building

- Which Frappe doctypes hold the source data for catalog sync (Item? a new mapping doctype)? See the two `talabat-product-mapping-*.csv` files on the Desktop — likely relevant to how SKUs map today.
- Where webhook payloads land — needs a whitelisted, `allow_guest` endpoint plus signature/auth verification (check the spec's webhook security scheme before making it public).
- Sandbox vs production credential storage — probably a Frappe Single doctype ("Talabat Settings") rather than raw `.env` vars, so it's editable from the UI per site.
