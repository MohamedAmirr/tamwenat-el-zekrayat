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
header credentials configured per destination in Noon's Integrator
Configuration UI. This is why `api/noon_webhooks.py` implements its own
secret-header check instead of verifying a signature: each `Noon Connection`
has its own `webhook_secret` (Password field), and every inbound request
must carry a matching `X-Noon-Webhook-Secret` header, compared with
`hmac.compare_digest`.

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
