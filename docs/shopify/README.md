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

## Webhooks (for #104)

- Custom apps (client-credentials, no Shopify CLI/`shopify.app.toml`) register webhook subscriptions via the GraphQL Admin API's `webhookSubscriptionCreate` mutation — pointing at any HTTPS URL, including a Frappe whitelisted endpoint. No Partner-app tooling or declarative config file needed.
- **HMAC verification needs no separate hosted app.** Shopify signs each delivery with `HMAC-SHA256(raw_request_body, client_secret)`, base64-encoded, in the `X-Shopify-Hmac-Sha256` header. Verifying it is a direct computation in Python using the same `client_secret` already stored on `Shopify Connection` — compare against the recomputed digest before parsing the body as JSON.
- Decision made: **no separate Node/Remix bridge app** for this (a scaffold exploring that shape exists at `github.com/MohamedAmirr/erpnext-shopify-integration` but is not the chosen path — see #104). ERPNext registers its own webhook subscription and verifies HMAC itself, mirroring `api/talabat_webhooks.py`'s pattern but with HMAC instead of Bearer-token auth.
- Client secret rotation note: Shopify signs webhooks with the app's *oldest unrevoked* client secret — during a rotation, verification must accept both the old and new secret until the old one is revoked.

Source: [Manage your app credentials](https://shopify.dev/docs/apps/build/authentication-authorization/client-secrets), [webhookSubscriptionCreate mutation](https://shopify.dev/docs/api/admin-graphql/latest/mutations/webhookSubscriptionCreate).

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

Still open:

- Exact scopes needed — depends on which resources later catalog/order work will touch (not decided yet). Recommended starting set once #104 needs them: `read_products`/`write_products`, `read_inventory`/`write_inventory`, `read_orders` (not `read_all_orders`, per #99's locked decisions).
- Whether the store domain format needed is `{store}.myshopify.com` or a custom domain — confirm against the specific store's Shopify admin before hardcoding validation. `shop_domain` currently accepts either shape.

Settled (research only, not yet acted on in #100):

- **App install flow**: in Dev Dashboard, open the app → Installs → Install app → pick the store (if the org has more than one) → Install. No OAuth approval screen.
- **Org-access caveat**: a custom app can only be installed on stores in the *same Shopify organization* as the Dev Dashboard account creating it. A known limitation: collaborator-level "Develop apps" access doesn't work with the new Dev Dashboard flow — installing needs org-level access, not just store-collaborator access. Confirm this before #100 hits a wall partway through.

Source: [Installing and setting up apps](https://help.shopify.com/en/manual/apps/install-setup-apps), [Collaborator "Develop apps" permission issue](https://community.shopify.dev/t/collaborator-develop-apps-permission-is-useless-with-new-dev-dashboard-jan-2026-cant-install-custom-app-on-clients-store/29171).

## References

- [Generate access tokens for admin-created custom apps](https://shopify.dev/docs/apps/build/authentication-authorization/access-tokens/generate-app-access-tokens-admin)
- [GraphQL Admin API reference](https://shopify.dev/docs/api/admin-graphql/latest)
- [REST Admin API reference (legacy)](https://shopify.dev/docs/api/admin-rest) — do not build new work against this
