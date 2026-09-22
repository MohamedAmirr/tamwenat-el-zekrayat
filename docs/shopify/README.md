# Shopify integration

No single official downloadable spec exists for Shopify (unlike Talabat's checked-in `openapi.json`) — Shopify does not publish one. This doc is a manually maintained reference, sourced from `shopify.dev`, scoped to what's needed for the current work: the `Shopify Connection` doctype and its auth layer (tracked by #101). Extend this doc (or add sibling files) when catalog/order sync work is actually scoped — don't infer those endpoints from here yet.

This will back `Shopify Connection` in the `marketplace_integrations` app — see `docs/marketplace-integrations/architecture.md`'s "Connection design decision" for why Shopify gets its own typed doctype rather than a shared one.

## App type: custom app, single store (Tamwenat only)

This is a **custom app** for one specific Shopify store — not a public app distributed to multiple merchants. No merchant-facing OAuth install flow is needed.

## Auth model (current, since Jan 1 2026)

Shopify no longer allows creating custom apps from the store Admin UI ("Develop apps") with a static token. New custom apps are created via the **Dev Dashboard** (`https://dev.shopify.com/dashboard/`), which issues a **Client ID** and **Client Secret** instead of a token you copy once.

Access tokens are obtained programmatically via the **client credentials grant**:

- `POST https://{store}.myshopify.com/admin/oauth/access_token`
- Body: `grant_type=client_credentials&client_id={client_id}&client_secret={client_secret}`
- Response: `access_token`, `scope`, `expires_in` (≈ 86399 seconds, i.e. ~24 hours)

Key difference from the legacy flow: the old `shpat_...` static token never expired. This client-credentials token does — it must be refreshed programmatically before/on expiry, similar in spirit to Talabat's token-cache-with-expiry-buffer pattern (`TalabatClient`), not a one-time setup value.

Apps created before Jan 1 2026 under the old flow keep their static token working, but any new custom app (this one) must use the client-credentials flow.

## API surface: GraphQL, not REST

Shopify marked the REST Admin API as legacy in Oct 2024. Since April 2025, new apps must use the **GraphQL Admin API** exclusively. Any Shopify client built here should be a GraphQL client, not a REST client — this is a structural difference from `TalabatClient`, which is REST-only.

## GraphQL Admin API (for #102)

- **Endpoint**: `https://{shop_domain}/admin/api/2026-07/graphql.json` — `POST` only.
- **Pinned API version**: `2026-07` (current stable as of this writing; supported until ~July 2027). `2026-10` is release-candidate until Oct 1 2026. Re-check `docs/api/usage/versioning` before pinning a new version later — don't assume this stays current.
- **Auth header**: `X-Shopify-Access-Token: {access_token}` (from `ShopifyClient.get_access_token()`), plus `Content-Type: application/json`.
- **Request body**: `{"query": "...", "variables": {...}}`.
- **Response body**: `{"data": {...}, "extensions": {"cost": {...}}}`.
- **Errors return HTTP 200.** The failure signal is a non-empty top-level `errors[]` array, not the status code — a 200 with data present and `errors` empty/absent is the only success shape.
- **Throttle shape**: `errors: [{"message": "Throttled", "extensions": {"code": "THROTTLED", "retryAfter": <seconds>}}]`. Use `extensions.retryAfter` directly for backoff — don't track bucket capacity/restore-rate ourselves, that varies by Shopify plan tier and isn't needed if we just honor `retryAfter`.
- **Per-mutation `userErrors`** are a separate concept from the top-level `errors[]` — they live inside the mutation's own payload (e.g. `webhookSubscriptionCreate.userErrors`) and must be surfaced to the caller, never treated as a transport failure.

Source: [GraphQL Admin API reference](https://shopify.dev/docs/api/admin-graphql), [Shopify API rate limits](https://shopify.dev/docs/api/usage/rate-limits), [GraphQL Admin API — example Throttled response](https://community.shopify.com/t/graphql-admin-api-example-throttled-requests-response/8456).

## Webhooks (#104, implemented)

- Custom apps (client-credentials, no Shopify CLI/`shopify.app.toml`) register webhook subscriptions via the GraphQL Admin API's `webhookSubscriptionCreate` mutation — pointing at any HTTPS URL, including a Frappe whitelisted endpoint. No Partner-app tooling or declarative config file needed. `Shopify Connection`'s "Sync Webhook Subscriptions" button (`integrations/shopify/webhooks.py`'s `sync_webhook_subscriptions()`) is the explicit, user-triggered mutation that registers/repairs subscriptions for every topic in `SHOPIFY_WEBHOOK_TOPICS` at the site's own callback URL, deleting any stale subscription left at an old URL first.
- **HMAC verification needs no separate hosted app.** Shopify signs each delivery with `HMAC-SHA256(raw_request_body, client_secret)`, base64-encoded, in the `X-Shopify-Hmac-Sha256` header. `api/shopify_webhooks.py::webhook_event()` verifies it (`verify_shopify_hmac()`) against the RAW request body before any JSON parsing, using the `client_secret` already stored on the resolved `Shopify Connection`.
- Decision made and implemented: **no separate Node/Remix bridge app** for this (a scaffold exploring that shape exists at `github.com/MohamedAmirr/erpnext-shopify-integration` but is not the chosen path — see #104). ERPNext registers its own webhook subscription and verifies HMAC itself, mirroring `api/talabat_webhooks.py`'s pattern but with HMAC instead of Bearer-token auth.
- Client secret rotation is handled: Shopify signs webhooks with the app's *oldest unrevoked* client secret, so `verify_shopify_hmac()` checks the connection's current `client_secret` first and, if that fails, its `previous_client_secret` — accepting either until the old secret is revoked and the field cleared.
- Every subscribed topic shares one endpoint (`webhook_event()`); `X-Shopify-Topic` (delivery header form, e.g. `orders/create`) is mapped to the GraphQL enum form (e.g. `ORDERS_CREATE`, via `SHOPIFY_WEBHOOK_TOPIC_HEADER_TO_ENUM`) to route to `process_shopify_webhook()`. Deduplication is by `X-Shopify-Webhook-Id`, enforced with a DB-level unique index on `Shopify Webhook Delivery.webhook_id`, not a check-then-insert read.
- The webhook request itself only authenticates, minimally validates shape, and enqueues — canonical order retrieval/ERPNext document creation from webhook payloads is out of scope for #104; every topic other than `APP_UNINSTALLED` (which disables the `Shopify Connection`) is acknowledged and recorded only.
- Because `webhook_event()` responds 200 as soon as processing is enqueued (correct: "respond promptly"), Shopify never retries a delivery whose async processing later fails. A scheduled hourly sweep (`retry_errored_webhook_deliveries()`, registered in `hooks.py`'s `scheduler_events`) reprocesses any `Shopify Webhook Delivery` still at `status="Error"`, bounded by a simple `attempt_count` cutoff (`MAX_WEBHOOK_DELIVERY_ATTEMPTS`) so a permanently-failing delivery isn't retried forever.

Source: [Manage your app credentials](https://shopify.dev/docs/apps/build/authentication-authorization/client-secrets), [webhookSubscriptionCreate mutation](https://shopify.dev/docs/api/admin-graphql/latest/mutations/webhookSubscriptionCreate).

## Products and variants (for #105)

- Catalog discovery uses a **flat `productVariants` root query** (single cursor), not nested `products { variants }` - simpler single-cursor pagination, matching `ShopifyClient.execute()`'s existing one-call-site pattern.
- `productVariants(first: Int, after: String, query: String)` returns a `ProductVariantConnection`: `pageInfo { hasNextPage endCursor }` plus `nodes`.
- `ProductVariant` fields selected: `id (ID!)`, `sku (String)`, `barcode (String)`, `product { id title }`. No variant-level `title` is selected - only the parent product's.
- Deleted-mapping detection uses the generic `nodes(ids: [ID!]!): [Node]!` root field (`... on ProductVariant { ... }`), the first batched/multi-id lookup in this codebase. Each response node is matched back to its saved mapping row by its own `id` field, never by list position - Shopify doesn't guarantee response order matches the requested id order. A deleted variant is one whose id is either missing from the response entirely or present with a `null` node.
- Source: [ProductVariant object](https://shopify.dev/docs/api/admin-graphql/latest/objects/ProductVariant), [productVariants query](https://shopify.dev/docs/api/admin-graphql/latest/queries/productVariants), [nodes query](https://shopify.dev/docs/api/admin-graphql/latest/queries/nodes) - all verified against shopify.dev on 2026-09-19 (external source, approved by the user for reliance).

## Open questions

`Shopify Connection` + `ShopifyClient` are now built (#101), and the Admin GraphQL `execute()` layer on top of it is now built (#102). What those issues settled and what's still open:

Settled by #101:

- Token refresh trigger: **proactive refresh before expiry**, mirroring `TalabatClient`'s safety-buffer pattern (`ttl = expires_in - EXPIRY_BUFFER_SECONDS`, cached per connection). The shared token-fetch/expiry-buffer math itself lives in `integrations/http.py`'s `fetch_client_credentials_token()`, used by both `ShopifyClient` and `TalabatClient`.
- `shop_domain` validation: `Shopify Connection.validate()` requires a bare hostname — only `[A-Za-z0-9.-]`, not starting/ending with a dot, no scheme/path/whitespace/port/userinfo/query/fragment. This is stricter than an earlier version that only blocked `://` and `/`, which still let an `@` through and made `get_store_base_url()` (a plain `https://` string prefix) vulnerable to userinfo/host confusion (e.g. `shop.myshopify.com@attacker.example` resolving to `attacker.example`, silently sending the client secret there). Deliberately does **not** hardcode a `.myshopify.com` suffix — see the still-open domain-format question below.

Settled by #102:

- Reactive token refresh: `ShopifyClient.execute()` retries exactly once on a 401 (`invalidate_access_token()` then re-fetch), mirroring `TalabatClient.request()`'s contract. A second 401 raises.
- `enabled` flag enforcement: `execute()` — the first real caller of `ShopifyClient` — now checks `Shopify Connection.enabled` and throws if disabled, closing the gap #101 deliberately left open.
- Throttle handling: bounded backoff honoring `errors[0].extensions.retryAfter`, capped at `MAX_THROTTLE_ATTEMPTS = 3` attempts — no bucket-capacity tracking.
- Error shape handling: a non-empty top-level `errors[]` (non-throttle) raises; per-mutation `userErrors` are returned to the caller inside `data`, never inspected or swallowed.

Settled by #104:

- Webhook transport/auth: a single whitelisted, guest-accessible endpoint (`api/shopify_webhooks.py::webhook_event()`) handles every subscribed topic, authenticated by `X-Shopify-Hmac-Sha256` over the raw body (never the reparsed/reserialized body).
- Dual-secret rotation handling: `verify_shopify_hmac()` accepts either the connection's current `client_secret` or its `previous_client_secret`, matching Shopify's "signs with the oldest unrevoked secret" behavior during a rotation window — this was an open question above and is now implemented, not just documented.
- Subscription management is a mutation, so it stays behind an explicit user-triggered action (`sync_webhook_subscriptions()`, the Shopify Connection "Sync Webhook Subscriptions" button) — never run automatically on save, unlike #103's read-only live checks.
- Deduplication: `Shopify Webhook Delivery.webhook_id` carries a DB-level `unique: 1` index (checked via `UniqueValidationError` on insert, not a check-then-insert read) — Shopify's at-least-once delivery guarantee means the same `webhook_id` can arrive more than once.
- Retry-ability of a failed delivery: neither a Shopify redelivery nor the "responds 200 immediately" requirement give up on a delivery whose async processing later failed — a non-`Processed` row is reprocessed in place (by a Shopify redelivery hitting the same `webhook_id`, or by the scheduled `retry_errored_webhook_deliveries()` sweep when no redelivery is coming), bounded by a simple `attempt_count` cutoff rather than retried forever.

Still open:

- Exact scopes needed — depends on which resources later catalog/order work will touch (not decided yet). Recommended set: `read_locations` (required by #103's Location-loading query — confirmed via a live "Access denied for name field" error, not something we predicted), `read_products`/`write_products`, `read_inventory`/`write_inventory`, `read_orders` (not `read_all_orders`, per #99's locked decisions).
- Whether the store domain format needed is `{store}.myshopify.com` or a custom domain — confirm against the specific store's Shopify admin before hardcoding validation. `shop_domain` currently accepts either shape.

Settled (research only, not yet acted on in #100):

- **App install flow**: in Dev Dashboard, open the app → Installs → Install app → pick the store (if the org has more than one) → Install. No OAuth approval screen.
- **Org-access caveat**: a custom app can only be installed on stores in the *same Shopify organization* as the Dev Dashboard account creating it. A known limitation: collaborator-level "Develop apps" access doesn't work with the new Dev Dashboard flow — installing needs org-level access, not just store-collaborator access. Confirm this before #100 hits a wall partway through.

Source: [Installing and setting up apps](https://help.shopify.com/en/manual/apps/install-setup-apps), [Collaborator "Develop apps" permission issue](https://community.shopify.dev/t/collaborator-develop-apps-permission-is-useless-with-new-dev-dashboard-jan-2026-cant-install-custom-app-on-clients-store/29171).

## References

- [Generate access tokens for admin-created custom apps](https://shopify.dev/docs/apps/build/authentication-authorization/access-tokens/generate-app-access-tokens-admin)
- [GraphQL Admin API reference](https://shopify.dev/docs/api/admin-graphql/latest)
- [REST Admin API reference (legacy)](https://shopify.dev/docs/api/admin-rest) — do not build new work against this
