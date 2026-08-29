# talabat_integration: code map

What actually exists in the app right now, so an agent can orient without grepping the whole tree. **Keep this current**: any PR that adds/removes/renames a module, doctype, or changes a core pattern described here must update this file in the same PR — see the "Keeping this updated" rule in `frappe-dev.md`/`frappe-review.md`.

## Doctypes

- **`Talabat Settings`** (Single) — `client_id`, `client_secret` (Password), `chain_id`, `environment` (Sandbox/Production). All four opaque-value fields set `ignore_xss_filter: 1` (see `frappe-conventions.md`). `get_base_url()` resolves the right host for the current `environment`.
- **`Talabat Vendor`** — registry of Talabat outlets/vendors. `vendor_id` (unique, `autoname: field:vendor_id`), `vendor_name`, `is_active`.
- **`Talabat Product Mapping`** — one row per (item, vendor) mapping to a Talabat SKU. Identity is currently `autoname: format:{item}-{vendor}` (issue #19, in review as of this writing, changes this to `format:{vendor}-{talabat_sku}` plus a hidden `item_vendor_key` field for DB-level secondary uniqueness — see that issue/PR for the reasoning; update this section once it merges). `talabat_sku` is intentionally separate from `Item.item_code` (Talabat's own `sku` is merchant-assigned per its API, not guaranteed to match). `sync_status`/`last_synced_on`/`last_error` track push-to-Talabat sync outcomes (not yet implemented — see #6).

## API layer (`talabat_integration/api/`)

- **`auth.py`** — `get_access_token()` (cached per-environment via `frappe.cache()`, refreshed with a safety buffer before real expiry), `invalidate_access_token()` (forces a fresh fetch after a 401). Also owns `_request()`, the one shared helper wrapping `requests` with a timeout and clean network-error handling — everything else routes through it, nothing else calls `requests` directly.
- **`client.py`** — `talabat_request(method, path, **kwargs)`: **the single choke point for every Talabat endpoint.** Injects the bearer token, retries exactly once on a 401 (via `invalidate_access_token()` + refetch), then raises if the retry also 401s. Returns the raw response as-is for anything else (200/4xx/5xx) — callers check `response.ok` themselves, since different endpoints use different success codes.
- **`catalog.py`** — whitelisted methods for Talabat's catalog. Currently: `get_vendor_products(vendor)`, fetching up to 500 products (Talabat's page-size cap) for a vendor, used to help pick a `talabat_sku` when creating a mapping. Does not persist anything Talabat returns (no stored title/price anywhere in this app) — catalog data is always live/display-only.

## Client-side (`talabat_integration/talabat_integration/doctype/talabat_product_mapping/talabat_product_mapping.js`)

A "Fetch from Talabat" flow: once `vendor` is set, calls `get_vendor_products`, offers the result in a dialog to fill `talabat_sku`. Fails gracefully (manual entry always still works) if the vendor has nothing yet or the fetch errors.

## Conventions specific to this app (see `frappe-conventions.md` for the general Frappe rules)

- Never call `requests` directly for a Talabat endpoint — always through `client.talabat_request()`.
- Never store a field's worth of data fetched live from Talabat (titles, prices, catalog contents) — display it, don't persist it, unless a future issue explicitly decides otherwise.
- Opaque external identifiers/secrets (`client_id`, `client_secret`, `chain_id`, `talabat_sku`) need `ignore_xss_filter: 1` *unless* the field is part of the doctype's `autoname` — Frappe's own name validation already rejects `<`/`>` for those before the sanitizer would ever run (verified for `vendor_id`, which is why it doesn't have the flag despite being an opaque identifier — see #17's review).
