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
  receiver acts on `FBPI::ORDER_SYNC` (queues the order import) and
  `RETURNS::REFERENCE_UPDATE` (queues a return lookup, see below); every other
  value is acknowledged (200) without being processed, so Noon stops retrying it.
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
- `payload` — the event-type-specific body, checked to be a JSON object. For
  `FBPI::ORDER_SYNC` only `order_nr` is read (#390), as a lookup key and never
  stored or logged: a non-empty string that matches a Marketplace Order of the
  resolved account (`external_order_id`) queues the deduplicated per-order
  `GetFbpiOrder` refresh (`orders.enqueue_order_refresh`); a missing, non-string
  or unknown value re-triggers the existing FBPI order-import job, which
  re-fetches state from Noon's own `ListFbpiOrders` endpoint. Nothing else in the
  payload is trusted. The sync log row gains `refresh_queued` (true for a new
  job, false for a deduplicated redelivery) when the refresh path is taken. For `RETURNS::REFERENCE_UPDATE` the
  receiver reads `barcode` only (#263, see below).

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
  `UpdateDestination` to subscribe `FBPI::ORDER_SYNC` only. If the second step fails
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
needs the subscribe step (it subscribes `FBPI::ORDER_SYNC` only). There is no delete endpoint: an unwanted destination
can only be deactivated, and it still counts toward the 5-destination limit.
A deactivated destination can still be subscribed (`UpdateDestination` 200).

Register, Subscribe and Update never touch `RETURNS::REFERENCE_UPDATE`; the
returns destination is created by hand in the Partner Center (see the returns
section below). When several destinations share this connection's URL, Refresh
Webhook Status and the destination lookup pick the one subscribed to
`FBPI::ORDER_SYNC`, or one with no event types yet; a destination on our URL whose
event types are non-empty and lack `FBPI::ORDER_SYNC` (a returns destination) is
never adopted as ours, though it still shows "This Site" in the table. Deleting destinations (deactivate instead) and editing other
destinations are not supported.

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

**Accepted gap, orders older than 7 days:** the order import only looks back
`ORDER_IMPORT_LOOKBACK_DAYS` (7) days from its start, and the webhook payload's
`order_nr` is not used to fetch a specific order. A webhook for an order created
more than 7 days ago therefore changes nothing; only a manual **Refresh from
Noon** on that Marketplace Order picks up the change.

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

## RETURNS::REFERENCE_UPDATE (#263)

Noon's event-type list for this project includes `RETURNS::REFERENCE_UPDATE`
(observed with `ListEventTypes` on the production account, 2026-10-15). Our
Register/Update never subscribe it. In production, create a second destination in
the Partner Center (same URL, `X-Api-Key` header holding the connection's webhook
key) subscribed only to `RETURNS::REFERENCE_UPDATE`.

If our webhook URL changes (Refresh Webhook URL, then Update Noon Destination),
Update pushes the new URL only to our order destination (`noon_destination_code`).
The hand-made returns destination keeps the old URL and silently stops receiving
events, so update it by hand in the Partner Center. The webhook key is never
regenerated, so the key is unaffected.

**Payload fields are UNVERIFIED.** The only description, `barcode` (string) and
`barcode_type` (`awb_nr` or `rms_barcode`), came from an AI-written summary of
Noon's docs, not from a Noon page or a real delivery. A web search on 2026-10-15
found nothing else, official or unofficial. The receiver therefore:

- reads only `payload.barcode`; when it is a non-empty string of at most 255
  characters it queues the existing `ListReturnReferences` lookup as a
  deduplicated `long`-queue job (`found_via` "Webhook", log `trigger_source`
  "Noon Webhook"), with no Noon call in the request;
- otherwise logs and ignores the event, and still answers 200 so Noon stops
  retrying;
- stores the raw `payload` on a `Marketplace Sync Log` row (`sync_type` Return
  Lookup, `request_data.payload`), capped at 4096 characters (larger payloads are
  kept as `truncated_payload`). Headers and the key are never stored. This is for
  this event only: `FBPI::ORDER_SYNC` logging is unchanged because its payload can
  hold customer data. Look at the first real payload before widening this.

**To verify after the first real delivery:** open the newest `Return Lookup` /
`Noon Webhook` row in Marketplace Sync Log, compare `request_data.payload` with
the assumed fields, and correct `api/noon_webhooks.py` and this section if they
differ. Until then the lookup is evidence capture only: it creates or updates
`Noon Return` rows and nothing in ERPNext.

