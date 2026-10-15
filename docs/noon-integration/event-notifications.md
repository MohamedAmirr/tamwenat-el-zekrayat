# Noon Event Notifications envelope (#163)

**Source note:** this document is sourced from Noon's public Event
Notifications page — https://noon-docs.noonpartners.dev/docs/api-reference/event-notifications
— which is **not** one of the downloadable API spec files already checked
into this repo (`noon_api_full.md`/`noon_api_full.yml`). It is internet-
sourced, and the user explicitly approved relying on it for this session's
work (#163). Treat it as a secondary reference, not the primary Noon API
contract — re-verify against a real Noon account/portal before depending on
any detail here beyond what #163 already built against it.

## Envelope shape

Every event notification Noon POSTs to a subscribed receiver uses one fixed
JSON envelope:

```json
{
  "event_schema_version": "...",
  "event_type": "FBPI::ORDER_SYNC",
  "metadata": {
    "message_id": "...",
    "destination_id": "...",
    "published_at": "...",
    "project_code": "..."
  },
  "payload": { ... }
}
```

- `event_schema_version` — a version marker for the envelope/payload shape.
  Not read or validated by `api/noon_webhooks.py` (#163 only checks the
  fields it actually needs).
- `event_type` — identifies what happened (e.g. `FBPI::ORDER_SYNC`). This
  receiver only acts on `FBPI::ORDER_SYNC`; every other value is
  acknowledged (200) without being processed, so Noon stops retrying it.
- `metadata.message_id` — a per-delivery identifier. Recorded on the
  `Marketplace Sync Log` row this receiver writes, but not used as a
  dedup key itself — see "Delivery semantics" below.
- `metadata.destination_id` — identifies which configured destination
  delivered this event. Not used by this receiver.
- `metadata.published_at` — when Noon published the event. Not used by this
  receiver.
- `metadata.project_code` — identifies which Noon project/connection this
  event belongs to. This receiver resolves it to exactly one enabled
  `Noon Connection`, and that connection to exactly one enabled `Marketplace
  Account`; otherwise the request is rejected.
- `payload` — the event-type-specific body. **Never parsed as business
  data by this receiver** — only checked to be a JSON object. Per-event-type
  payload handling is explicitly out of scope for #163 (a follow-up once a
  real payload schema is captured); the receiver currently only re-triggers
  the existing FBPI order-import job, which re-fetches state from Noon's own
  `ListFbpiOrders`/`GetFbpiOrder` endpoints rather than trusting anything in
  this payload.

## Authentication

Noon's Event Notifications have **no HMAC/signature scheme** — only static
header credentials configured per destination in Noon's Partner Center,
Integrator Configuration, Webhook tab. This is why `api/noon_webhooks.py` implements its own
secret-header check instead of verifying a signature: each `Noon Connection`
has its own `webhook_secret` (Password field, auto-generated when empty), and
every inbound request must carry a matching `X-Api-Key` header (the header
Noon's Partner Center, Integrator Configuration, Webhook tab sends), compared with `hmac.compare_digest`.
The old `X-Noon-Webhook-Secret` header is no longer accepted.

## Observed on the live sandbox test

Observed during a sandbox test, not from Noon's docs:

- The Webhook tab sends the key in the `X-Api-Key` header.
- The delivered payload only contains `order_nr`
  (`{"order_nr": "<fbpi_order_nr>"}` inside the envelope's `payload`).
- A bare `{"order_nr": "..."}` body (no `event_type`/`metadata`/`payload`)
  arrived once, right after saving the destination config; the cause is
  unconfirmed. The receiver authenticates it by matching `X-Api-Key` against
  every enabled Noon Connection's secret, returns 200 and enqueues nothing.
  A missing or wrong key is rejected.
- The first delivery attempt arrived about 27 seconds after the event.
- An old event was redelivered about 7 minutes later.
- Source IPs varied (34.76.219.109, 35.233.95.217, 104.155.39.2), so do not
  allowlist by IP.

### Other sandbox observations (observed in a sandbox test on 2026-10-07)

Observed in a sandbox test on 2026-10-07, not Noon contract:

- `CreateSandboxOrder`: an `idempotency_key` longer than 10 characters is
  rejected (a 36-character UUID returned 400 `INVALID_ARGUMENT`). `items`
  has one entry per unit.
- Sandbox orders use `mp_code` `test_integration_mp` and order numbers of the
  form `TEST-<merchant>-<10 hex>`. This app skips any order whose `mp_code`
  is not `"noon"`, so sandbox orders are not imported. Sandbox support is
  deliberately out of scope.
- Destination credential values are always redacted on read.
- `order_created_at` has no timezone suffix; it matched UTC.
- `delivered_invoice_price` on sandbox orders is a dummy value.

## Registering the destination from the Noon Connection (#253)

The Noon Connection form can create and manage its own destination, so the
Partner Center no longer has to be edited by hand. Actions appear by cached
webhook status (use Refresh Webhook Status first):

- **Register Webhook in Noon** (status "Not registered"): confirms, then
  `CreateHttpsDestination` with name `<project_code>-webhook`, the connection's
  webhook URL and `credentials.values = {"X-Api-Key": <webhook_secret>}`, then
  `UpdateDestination` to subscribe `FBPI::ORDER_SYNC`. If the second step fails
  the destination is kept and the status becomes "Registered, not subscribed".
  Refused when Noon already holds 5 destinations (Noon's per-project limit).
- **Subscribe to Order Sync** (status "Registered, not subscribed").
- **Update Noon Destination** (any known destination): sends the current URL;
  the key is sent only when the "Also send the current webhook key" box is
  ticked. Omitting `credentials` leaves Noon's stored key unchanged.
- **Activate / Deactivate Destination**: toggles destination-level `is_active`.
- **Noon Destinations table** (#258): Refresh Webhook Status caches every destination Noon holds (name, URL, active, event types, which one is this site's) from the same `ListDestinations` call. The row button activates or deactivates any listed destination after a confirm. Other destinations can only be toggled, not edited; deleting is not possible (Noon has no delete endpoint).

Observed on the live account (read plus one idempotent Update, 2026-10-15,
not Noon contract): `ListDestinations` returns `event_types` as a list of
strings, e.g. `["FBPI::ORDER_SYNC"]`; `UpdateDestination` answers HTTP 200
with body `{}`; the Static Sandbox project had no destinations and an empty
event-type list, so subscribing could not be exercised there. The
`CreateHttpsDestination` answers HTTP 200 with `{"destination_code": "..."}`
and creates the destination **active with no event types**, so Register always
needs the subscribe step. There is no delete endpoint: an unwanted destination
can only be deactivated, and it still counts toward the 5-destination limit.
A deactivated destination can still be subscribed (`UpdateDestination` 200).

Deleting destinations (deactivate instead), other event types and multiple destinations per
connection are not supported.

## Delivery semantics

Noon uses **at-least-once delivery**, with **up to 50 retries using
exponential backoff** on a non-2xx (or otherwise failed) response. This
receiver's authentication/envelope-shape checks finish synchronously so an
invalid request fails fast (and predictably) across those retries; a valid,
handled event enqueues the existing per-account deduplicated
`run_order_import` job (same `job_id` scheme as #145's polling/manual
paths), so redelivery of the same event (or a burst of several events for
the same account) can never queue more than one import job per account at a
time.

**Accepted dedup gap:** if a webhook event arrives while that account's
import job is already queued/running, RQ's own `deduplicate=True` makes
`frappe.enqueue(...)` return `None` for this delivery - no new job is
queued. The receiver still authenticates and validates the envelope
successfully, so it still returns 200 and logs `status="Success"` (with
`import_queued: false` recorded in the log row's `request_data`) - Noon
correctly stops retrying a delivery this app has in fact accepted. The
order this event was about is not actually (re-)imported by this specific
delivery, though: it is only picked up whenever the already-running/queued
import job happens to run, or - if that job's own lookback window already
passed before this event's change landed on Noon's side - by the next
hourly poll cycle (#145), up to about an hour later. This is accepted
behavior, not a bug: it follows directly from relying entirely on #145's
existing per-account job_id dedup rather than introducing a new dedup
doctype/mechanism for this receiver (see "Delivery semantics" above).
