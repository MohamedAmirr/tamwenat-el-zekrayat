# talabat_integration: code map

What actually exists in the app right now, so an agent can orient without grepping the whole tree. **Keep this current**: any PR that adds/removes/renames a module, doctype, or changes a core pattern described here must update this file in the same PR — see the "Keeping this updated" rule in `frappe-dev.md`/`frappe-review.md`.

## Doctypes

- **`Talabat Settings`** (Single) — `client_id`, `client_secret` (Password), `chain_id`, `environment` (Sandbox/Production). All four opaque-value fields set `ignore_xss_filter: 1` (see `frappe-conventions.md`). `get_base_url()` resolves the right host for the current `environment`.
- **`Talabat Vendor`** — registry of Talabat outlets/vendors. `vendor_id` (unique, `autoname: field:vendor_id`), `vendor_name`, `is_active`.
- **`Talabat Product Mapping`** — one row per (vendor, Talabat SKU) mapped to an ERPNext Item. Identity is the SKU+vendor pair (the stable thing each row represents), via a custom `autoname()` controller method: `name = f"{vendor}::{talabat_sku}"` — `::` because a plain `-` genuinely collides (item codes and Talabat vendor/chain IDs are hyphen-heavy; see `frappe-conventions.md`'s autoname bullet). `validate()` rejects `::` appearing in either field, enforcing the invariant the naming scheme depends on. Secondary (item, vendor) uniqueness is a hidden `item_vendor_key` field (`frappe.as_json([item, vendor])`, not a joined string — same collision reasoning), computed in `before_save()`, with a DB-level unique index and a pre-existing-row backfill patch (`patches/v0_0/`). `talabat_sku` is intentionally separate from `Item.item_code` (Talabat's own `sku` is merchant-assigned per its API, not guaranteed to match). `sync_status`/`last_synced_on`/`last_error` track push-to-Talabat sync outcomes (not yet implemented — see #6).

## API layer (`talabat_integration/api/`)

- **`auth.py`** — `get_access_token()` (cached per-environment via `frappe.cache()`, refreshed with a safety buffer before real expiry), `invalidate_access_token()` (forces a fresh fetch after a 401). Also owns `_request()`, the one shared helper wrapping `requests` with a timeout and clean network-error handling — everything else routes through it, nothing else calls `requests` directly.
- **`client.py`** — `talabat_request(method, path, **kwargs)`: **the single choke point for every Talabat endpoint.** Injects the bearer token, retries exactly once on a 401 (via `invalidate_access_token()` + refetch), then raises if the retry also 401s. Returns the raw response as-is for anything else (200/4xx/5xx) — callers check `response.ok` themselves, since different endpoints use different success codes.
- **`catalog.py`** — whitelisted methods for Talabat's catalog. Currently: `get_vendor_products(vendor)`, fetching up to 500 products (Talabat's page-size cap) for a vendor, used to help pick a `talabat_sku` when creating a mapping. Does not persist anything Talabat returns (no stored title/price anywhere in this app) — catalog data is always live/display-only.

## Client-side (`talabat_integration/talabat_integration/doctype/talabat_product_mapping/talabat_product_mapping.js`)

A "Fetch from Talabat" flow: once `vendor` is set, calls `get_vendor_products`, offers the result in a dialog to fill `talabat_sku`. Fails gracefully (manual entry always still works) if the vendor has nothing yet or the fetch errors.

## Conventions specific to this app (see `frappe-conventions.md` for the general Frappe rules)

- Never call `requests` directly for a Talabat endpoint — always through `client.talabat_request()`.
- Never store a field's worth of data fetched live from Talabat (titles, prices, catalog contents) — display it, don't persist it, unless a future issue explicitly decides otherwise.
- Opaque external identifiers/secrets (`client_id`, `client_secret`, `chain_id`, `talabat_sku`) need `ignore_xss_filter: 1`. The one exception is a field that is *itself* the entire autoname source and can never be edited after creation without changing the record's identity — e.g. `Talabat Vendor.vendor_id` (`autoname: field:vendor_id`), where `_sync_autoname_field` reverts any edit anyway, so the sanitizer never gets a meaningful chance to run. This exception does **not** extend to a field that merely contributes to a `format:`/custom `autoname()` alongside other fields (like `talabat_sku`) — `name` isn't recomputed on update for those, so an edit to the field goes through the sanitizer unprotected unless it has the flag. Verified the hard way: #19 initially over-generalized this and had to add the flag back.
