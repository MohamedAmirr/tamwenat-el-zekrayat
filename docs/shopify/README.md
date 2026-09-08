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

## Open questions

`Shopify Connection` + `ShopifyClient` are now built (#101). What that issue settled and what's still open:

Settled by #101:

- Token refresh trigger: **proactive refresh before expiry**, mirroring `TalabatClient`'s safety-buffer pattern (`ttl = expires_in - EXPIRY_BUFFER_SECONDS`, cached per connection). Reactive refresh on a 401 is deferred to #102, since there's no `request()`/GraphQL call layer yet to retry on. The shared token-fetch/expiry-buffer math itself now lives in `integrations/http.py`'s `fetch_client_credentials_token()`, used by both `ShopifyClient` and `TalabatClient`.
- `shop_domain` validation: `Shopify Connection.validate()` requires a bare hostname — only `[A-Za-z0-9.-]`, not starting/ending with a dot, no scheme/path/whitespace/port/userinfo/query/fragment. This is stricter than an earlier version that only blocked `://` and `/`, which still let an `@` through and made `get_store_base_url()` (a plain `https://` string prefix) vulnerable to userinfo/host confusion (e.g. `shop.myshopify.com@attacker.example` resolving to `attacker.example`, silently sending the client secret there). Deliberately does **not** hardcode a `.myshopify.com` suffix — see the still-open domain-format question below.
- `enabled` flag enforcement: `ShopifyClient.get_access_token()` does **not** check `Shopify Connection.enabled` yet. This is currently unreachable (no caller exists in #101), but it is not forgotten — whichever future issue (#102/#103) adds the first real caller (GraphQL `request()`/`execute()`) must add that check at the same time.

Still open:

- Exact scopes needed — depends on which resources later catalog/order work will touch (not decided yet).
- Whether the store domain format needed is `{store}.myshopify.com` or a custom domain — confirm against the specific store's Shopify admin before hardcoding validation. `shop_domain` currently accepts either shape.
- The GraphQL Admin API endpoint path and version to pin against — not yet documented in this repo; blocks #102's `execute()`/query layer.
- Shopify's webhook registration/verification mechanism — not yet documented in this repo; blocks #104.

## References

- [Generate access tokens for admin-created custom apps](https://shopify.dev/docs/apps/build/authentication-authorization/access-tokens/generate-app-access-tokens-admin)
- [GraphQL Admin API reference](https://shopify.dev/docs/api/admin-graphql/latest)
- [REST Admin API reference (legacy)](https://shopify.dev/docs/api/admin-rest) — do not build new work against this
