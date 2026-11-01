# Noon API reference snapshot

`noon_api_full.md` and `noon_api_full.yml` are a 2026-09-29 AI-normalized
snapshot of the public [Noon API documentation index](https://noon-docs.noonpartners.dev/llms.txt).
They are reference material, not an official OpenAPI specification. The Markdown
file is copied unchanged from the supplied snapshot. The YAML file is copied
with its 51 empty or null `source_url` values restored from the matching
endpoint sections of the Markdown file; the remaining supplied content is
unchanged.

The snapshot has extraction gaps. Many structured `fields` and `rate_limit`
values are the literal string `[object Object]`; those strings are not usable
schemas or quotas. The `BatchChildSkuRename` entry has no method or path and
explicitly marks its request and response schemas unavailable. The API login
entry does not expose a usable response schema. Consult each endpoint's
`source_url` and the official guides before implementing it, and leave any
unavailable value marked as unknown rather than filling it by inference.

For issue #143's authentication flow, also consult the official
[Quick Start](https://noon-docs.noonpartners.dev/docs/getting-started/quick-start),
[Authenticating Your Requests](https://noon-docs.noonpartners.dev/docs/authentication/authenticating-requests),
[Getting Your Credentials](https://noon-docs.noonpartners.dev/docs/authentication/getting-credentials),
[Errors](https://noon-docs.noonpartners.dev/docs/overview/errors),
[Rate Limits & Usage](https://noon-docs.noonpartners.dev/docs/overview/rate-limiting),
and [Static Sandbox](https://noon-docs.noonpartners.dev/docs/overview/sandbox)
guides. These guides are online sources, not copied into this snapshot.

## Noon shipment from the Delivery Note (#415)

The Delivery Note of a Noon Sales Order is the shipment. The Noon adapter
(`integrations/noon/delivery_note_shipment.py`) plugs into the neutral carrier contract
(`services/carrier_shipments.py`); see `docs/marketplace-integrations/delivery-note-first.md`.

- **Create on Submit.** The form waits for a background job ("Sending to Noon...") and submits only once
  the Integration Status is Created. The job asks Noon first (`GetShipment`) on every attempt, the first
  included; only a confirmed "No shipment found" allows `CreateShipment`. A timeout, 5xx, 408/429 or an
  unrecognised answer is Creation Unknown (Check Noon, then Submit again); a definite 4xx is Creation Failed.
  Nothing is retried automatically.
- **Shipment number** = the Delivery Note name, stored in Carrier Reference at the first attempt. An amended
  note has a new name, so a new number.
- **AWB.** Type it (sent with courier `noon`), or tick Generate AWB through Noon under it: the job calls
  `GetNoonLogisticsAWBs` once (qty 1), writes the AWB to the note and logs an Integration Request row
  (operation AWB) before `CreateShipment`. A retry reuses the stored AWB. An AWB held by another Delivery Note
  that is not cancelled is refused, ignoring case.
- **Units** come from the order's latest payload, by SKU and quantity (Confirmed and Acknowledged only).
  Units that another live Noon note started are read from that note's request snapshot.
- **No COD, no address, no Shipping Rule.** The carrier is the locked Supplier Noon; the note clears its
  Shipping Rule and the tax row that rule added.
- **Cancel.** Noon is asked first (`GetFbpiOrder`): every unit of the shipment already cancelled is success
  with no cancel call. Otherwise `CancelShipment`: 200 cancels, a definite 4xx leaves the note submitted with
  Noon's reason, anything else is Cancellation Unknown (Retry asks Noon first again). The ERPNext cancel
  follows only after Noon confirms. Changing the AWB or items of a created shipment is cancel and amend.
- **Not built yet (phase 3 and later):** Shipping Status refresh, the item status column values, the
  webhook refresh and the nightly Noon reconcile (placeholders do nothing), and the FBPI warehouse check
  (hook `check_fbpi_warehouse`, sub-issue #416).
