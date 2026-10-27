# Marketplace Order sync status

How `Marketplace Order.erpnext_sync_status` is decided, which cases end in which status, and which of them the app resolves by itself. The implementation is in `services/sync_reason_codes.py`, `services/sales_order_reconciliation.py` (`plan()`), `services/sales_order_sync.py`, `services/sales_order_sync_debounce.py` and `services/sales_order_recheck.py`; the shared architecture is in [`architecture.md`](architecture.md). The case numbers 1-20 below are the inventory of what `plan()` and the sync service can report for a Sales Order that is already submitted.

## Status model

| Status | Meaning | Expected behavior |
|---|---|---|
| Syncing | A marketplace or ERPNext change is known and processing is pending or running, including a temporary retry. A new import starts here. | The sync runs after the 45 second debounce. Transient failures are retried (see below). |
| Synced | ERPNext matches the intended marketplace state, or an approved reconciliation has been completed. | No staff action. |
| Sync Error | The integration cannot reliably build, interpret or execute the sync: invalid or missing integration data, or a technical failure. | Shows the concrete error. Recoverable problems are retried or re-synced automatically. |
| Needs Action | The desired change is understood, but applying it needs a human operational or accounting decision, approval, physical verification, or manual correction of conflicting ERP documents. | Shows the required action and any draft the app prepared. |
| Cancelled | Terminal: the order was cancelled before any Sales Order existed (#353): a Shopify cancellation (unpaid or fully refunded), or a Noon order whose every unit is cancelled. | Excluded from every sweep and catch-up. |

"Not Synced" and "Modified After Sync" no longer exist. The migration patch moves legacy "Not Synced" rows as follows: a row created within the last 30 days (`LEGACY_NOT_SYNCED_RECENT_DAYS`, measured from `creation`, the import time) that is also eligible is set to Syncing and bumped; every other row becomes Needs Action with `LEGACY_NOT_SYNCED_REVIEW`. Eligible means: Talabat always; Noon when the order has at least one eligible unit; Shopify when it is not cancelled and its financial status is PAID or PARTIALLY_PAID, or a manual payment is pending. Sales Orders stay auto-submitted at import. The 45 second debounce is unchanged and covers webhooks, polls, sweeps and form edits.

### Choosing between Sync Error and Needs Action

1. Can the app reliably resolve the marketplace order, the item identities and mappings, the prices and the target ERP state? If not (disabled or missing mapping, missing price, a snapshot that cannot be built), the status is Sync Error.
2. If the desired state is known, can the app apply it safely within ERPNext and marketplace lifecycle constraints? If yes it applies it and ends Synced. If not, because stock or money has moved, an advance or draft exists, a person made a conflicting choice or accounting judgment is needed, the status is Needs Action.
3. A transient network, API or worker failure keeps the order Syncing while bounded retries are pending. It becomes Sync Error only after the retries are used up.
4. Two data-integrity ambiguities (cases 5 and 11) are Needs Action on purpose: retrying cannot resolve them and a person must investigate.
5. Neither every technical exception is Needs Action nor every blocked business operation Sync Error. The machine-readable reason code is recorded independently of the visible status.

| Scenario | Status | Reason |
|---|---|---|
| Unmapped or disabled mapping | Sync Error | The ERP item cannot be resolved |
| Missing required price | Sync Error | No valid target snapshot can be built |
| Temporary timeout, retry still scheduled | Syncing | Recoverable processing in progress |
| Timeout after the retries are used up | Sync Error | Persistent technical failure |
| Cancelled marketplace order already delivered | Needs Action | Compensating stock transaction and receipt review |
| Refunded order already invoiced | Needs Action | Credit Note or invoice treatment is an accounting decision |
| Advance payment prevents cancellation | Needs Action | A person must resolve the advance |
| Shipping charge changes on a submitted order | Needs Action | No approved safe amendment path |
| Duplicate or missing snapshot line identity | Needs Action | Data-integrity review |
| Shopify order cancelled after payment, before any Sales Order | Needs Action | Refund the customer in the marketplace |
| Legacy "Not Synced" row that is older than 30 days or not eligible (migration) | Needs Action, `LEGACY_NOT_SYNCED_REVIEW` | Review it; use Retry Sync if it is still needed |

## Reason codes and metadata

`services/sync_reason_codes.py` maps each reason code to its visible status, remediation mode, action type and default instruction (translated when used). `plan()` returns a `reason_code` for every result other than NO_ACTION; the cancel, close and update outcomes carry none until an execution fails. Callers never choose the status directly.

Remediation modes:

- `auto_resolve`: the app applies the correction and ends Synced.
- `auto_prepare`: the app prepares a draft for review, never submits it, and the order stays Needs Action until a person finishes the work.
- `manual`: no corrective transaction; Needs Action with an instruction.

The visible status and the remediation are separate fields on Marketplace Order (ERPNext Sync tab):

| Field | Content |
|---|---|
| `sync_reason_code` | Stable machine code, for example `RETURN_REQUIRES_RECEIPT_REVIEW` |
| `sync_action_type` | What a person should do, for example `REVIEW_RETURN_DRAFT` |
| `sync_remediation_mode` | `auto_resolve`, `auto_prepare` or `manual` |
| `sync_action_state` | Waiting for User, Awaiting Receipt, Draft Prepared, Recheck Queued, Resolved |
| `sync_blocking_doctype`, `sync_blocking_document` | The document in the way, when known |
| `sync_prepared_doctype`, `sync_prepared_document` | The draft the app prepared; filled only after it was created |
| `sync_retry_count` | Hidden: transient retries used |
| `receipt_confirmed_by`, `receipt_confirmed_on` | Who confirmed the goods were received, and when |
| `last_sync_error` | The user-facing instruction or error text |

A prepared document is never shown as a completed return. A draft reference is stored only after the draft exists.

## Cases by group

### Group A: structural changes on a submitted order without delivery, billing or advance

| # | Case | Handling |
|---|---|---|
| 6 | A line went from zero back to a positive quantity | Needs Action. `allow_zero_qty_in_sales_order` is 0, so a zeroed line is deleted from the Sales Order; re-adding it is a separate, later option. |
| 9 | Shipping charge or another header change | Needs Action |
| 10 | Item added or removed | Auto-resolve: `_execute_update` adds rows and removes rows by omission through ERPNext's `update_child_qty_rate`. A row ERPNext refuses (delivered, ordered, in a work order, billed) makes the whole order Needs Action. |
| 14 | Item, UOM or warehouse changed (mapping or account change) | Needs Action |

Quantity and rate changes ERPNext's validation permits are auto-resolved. Shipping-charge and item-swap changes would need a cancel and amend, which creates a new Sales Order name and affects Marketplace Order, Bosta and Noon links; that is not approved for automatic execution.

### Group B: missing baseline or bad snapshot

| # | Case | Handling |
|---|---|---|
| 4 | No stored baseline snapshot | Auto-resolve only when the current marketplace state and ERPNext are proven equivalent (the snapshot pairs with the Sales Order rows, `plan()` reports NO_ACTION, and for Shopify the order total matches); the baseline is then adopted. Otherwise Needs Action. |
| 5 | Duplicate or missing snapshot line identities | Needs Action |
| 11 | The Sales Order no longer represents the reconciled marketplace state | Needs Action |

The app never invents a missing identity, overwrites an ambiguous baseline or claims Synced without proving equivalence.

### Group C: stock or money already moved

| # | Case | Handling |
|---|---|---|
| 1 | Return on any order, or refund on an order with delivery or billing | Auto-prepare behind the receipt gate |
| 2 | Cancelled and fully delivered | Auto-prepare behind the receipt gate |
| 3 | Cancelled after fulfillment, delivered in ERPNext | Auto-prepare behind the receipt gate |
| 8 | Cancelled after fulfillment, billed but not delivered | Needs Action |
| 12 | All lines zero, already delivered | Needs Action |
| 15 | Requested quantity below the delivered quantity | Needs Action |
| 16 | Already billed, Sales Invoice exists | Needs Action (accounting decision) |
| 17 | Delivery or billing blocks the submitted-order update | Needs Action |

**Receipt gate.** A marketplace `return`, `refund` or `cancelled` event does not prove goods were received, and neither does a Bosta status. The order waits in Needs Action with action state Awaiting Receipt. A user with the Stock Manager or System Manager role who also has write permission on Marketplace Order (a platform role such as Talabat User or Noon User; Stock Manager alone is not enough) presses **Confirm Goods Received** on the Marketplace Order (the whitelisted method checks the role and the write permission on the server and records `receipt_confirmed_by` and `receipt_confirmed_on`). The app then:

- for Talabat and Shopify, creates one draft return Delivery Note with ERPNext's `make_sales_return` from the order's one submitted Delivery Note, links it in `sync_prepared_*` and sets Draft Prepared;
- for Noon, records the receipt acknowledgement on the linked Noon Return (its existing inspection flow) and links that Noon Return; its inspection step creates the draft Sales Return.

A second click or a repeated event creates no second draft: an existing draft or submitted return for the same Delivery Note is reused. Nothing is submitted, and Credit Notes stay manual. When a person submits the prepared return, the order is re-checked and treats the return as the compensating transaction.

### Group D: a manual choice or another ERP document blocks the change

| # | Case | Handling |
|---|---|---|
| 7 | Sales Order Closed or Cancelled while the marketplace expects fulfillment | Needs Action |
| 13 | Marketplace lifecycle forbids the change (for example Noon after shipping) | Needs Action |
| 18 | Cancellation with an advance payment | Needs Action |
| 19 | Updated total would fall below the advance | Needs Action |
| 20 | A draft Delivery Note or Payment Entry blocks cancellation | Needs Action |

The app does not override a deliberate manual close, delete drafts or reallocate advances.

## Transient failures

Timeouts, lock or deadlock errors, HTTP 5xx and 429 in the sync path are retried up to three times, after 1, 5 and 15 minutes. The retry rides on `sync_debounce_until` and `sync_retry_count`, so the minute sweep and its `job_id` deduplication run it, and the order shows Syncing while a retry is pending. After the third retry the status becomes Sync Error with `SYNC_RETRIES_EXHAUSTED`. Terminal failures (missing mapping or price, validation errors) go straight to Sync Error without a retry. A success resets the count; a manual Retry Sync gives a fresh budget.

## Re-checking without a button press

A person should not need to press a button after fixing a blocker. Two triggers feed the normal debounced sync:

- Document events: a Delivery Note, Sales Invoice or Payment Entry submit, cancel or delete, or a Sales Order update after submit or cancel, queues a re-check of the linked orders that are in Needs Action or Sync Error (matched through the Sales Order link or as the recorded blocking or prepared document). The order shows Recheck Queued and the trigger source is "ERPNext Document Change". The handler never raises and makes no HTTP call. The debounce bump itself is queued with `enqueue_after_commit`, so it runs only if the submitter's transaction commits and never writes inside it. The order whose sync is running is ignored only when that sync runs in the same process; a sync running in a worker is not seen, and the debounce window it leaves absorbs the bump.
- Mapping save: saving an enabled Marketplace Product Mapping bumps the account's orders in Sync Error with a mapping or price reason code.

The hourly sweep is the fallback for missed events, imports and a Close or Reopen. It re-checks Needs Action and Sync Error orders at most once a day. The manual **Retry Sync** button runs the same guarded path and is a fallback, never a required step.

Completion rules: a resolved blocker does not force Synced if another blocker remains; the normal planner runs again against fresh marketplace and ERPNext state. Repeated events and Retry Sync cannot duplicate ERP documents: one `job_id` per order, the order row is locked during a sync, and drafts are looked up before one is created.

| Needs Action example | Re-check trigger | Verified before Synced |
|---|---|---|
| Advance blocks cancellation or update | Payment Entry or Sales Order change | The advance no longer blocks; the update completes |
| A draft Delivery Note or Payment Entry blocks cancellation | The draft is submitted, cancelled or deleted | No draft remains; drafts are never deleted automatically |
| Sales Order corrected manually | Sales Order change | The order matches the marketplace state and is valid |
| Prepared Sales Return | The return is submitted | A person confirmed the receipt; the planner no longer reports return evidence as unresolved |
| Billed order | Sales Invoice or Credit Note change | The difference between marketplace and ERPNext is resolved; no automatic choice between cancel and Credit Note |
| Marketplace lifecycle forbids the update | A new marketplace state or the sweep | The restriction cleared |

## Echo of the app's own actions

The Talabat webhook queues a sync only when the order is new or its evidence fingerprint changed, so the echo of our own PUT (whose response was already stored) does not raise Syncing. The Shopify item-edit and cancel markers, the Talabat cancel marker and the Bosta timestamp guard are unchanged.

### Expected-change marker (Talabat and Noon)

An outbound call that changes the platform order makes the platform report the new state back on the next webhook, import or poll. Without a guard that report looks like a platform-side change and re-triggers the sync. The guard works as follows.

- **Marker.** After the platform accepts the call, the order stores `expected_change_hash` (a SHA-256 of the state we sent) and `expected_change_scope` (what the hash covers). Both fields are hidden and read-only. They are separate from the Shopify `outbound_edit_fingerprint` and `outbound_edit_state` pair and from `outbound_cancelled_on`, which keep working as before. The marker is written after the HTTP call returns; the caller commits it before anything that can fail, like `outbound_cancelled_on` (Talabat: `request_platform_cancel` and the `update_order` API commit right after the send; Noon: the worker's next commit, before its refresh). Noon's worker holds the order row lock only until that commit and takes no lock across the HTTP call. Talabat's `request_platform_cancel` already held the row lock across its PUT before this change, and still does. No save of a document changes the marker in either direction: the controller copies the stored values back on every save, so a stale copy can neither blank a marker nor bring back a cleared one.
- **Next import.** The import paths hash the incoming platform state over the same scope and compare. Equal means the change is our own echo. The marker is cleared and the order is saved without the `before_save` re-plan (so no Syncing, no Needs Action from the echo) and without a debounce bump. Different means a real platform change: the marker is cleared and the import runs the normal path. No marker means the normal path, unchanged.
- **The Sales Order is still brought up to date.** On an echo of a linked order whose evidence changed, the importer queues a short job (`echo_guard.run_echo_sync`, `enqueue_after_commit`, one job per order) that runs `sync_sales_order`, so the quantity or cancellation is applied and the baseline advances without a debounce. It runs only after the import has committed, so a failure in the sync can never roll the import back, lose the webhook payload or bring the marker back, and no webhook worker or request runs a full sync itself. If the job's sync raises, the order falls back to the normal debounce. A genuine planner result such as a delivered line is still reported as it is today.
- **Our own refresh.** The immediate re-import after our send does not consume the marker: the Talabat PUT response is upserted directly (not through the guard), and Noon's refresh after Mark Out of Stock and its pre-shipment refresh in Create Shipment call `import_order(echo_guard=False)`. Only a later webhook, import, poll or manual refresh (Talabat `refresh_order`, `import_vendor_orders`) consumes it.
- **New orders are never left Syncing.** A new order starts Syncing, so every import path bumps the debounce for it: the Talabat webhook, `refresh_order` and `import_vendor_orders` (new or changed evidence), and the Noon import even when the new order has no eligible unit. An order that can never sync then ends in a final state through the normal path, not in Syncing: a new Noon order whose every unit is CANCELLED, with no Sales Order, becomes the terminal Cancelled status (the same path as a Shopify cancellation before sync, no Sales Order created); a Noon order with no eligible unit that is not fully cancelled (for example units out of stock) ends in Sync Error with the reason.
- **Only the later echo is guarded.** A Needs Action raised by our own immediate refresh (the `update_order` upsert, `_upsert_returned_order`, or the Noon own refresh) is not suppressed: the guard changes how the later webhook, import or poll is treated, nothing else.
- **A second echo while the sync job is queued or running** falls back to the normal debounce bump, because the queue drops a second job with the same id and that job may have read the state before the echo.
- **Unlinked orders.** An order with no Sales Order has nothing to protect; its marker is cleared and the normal path runs.

What is hashed, per platform:

| Platform and call | Sent state | Incoming state compared |
|---|---|---|
| Talabat cancel PUT | Order status `CANCELLED` and the cancellation reason | `external_status` and `cancellation_reason` |
| Talabat UPDATE_CART | Order status `UPDATE_CART` and, per item keyed by SKU: pricing type, status class, quantity (UNIT) or weight (KG) | `external_status` and, per item keyed by `external_sku`: pricing type, status class, `fulfilled_quantity` or `fulfilled_weight` |
| Noon Mark Out of Stock | The sent `mp_item_nr` values, each expected `OUT_OF_STOCK` (the unit numbers are also stored in `expected_change_scope`) | The `integration_status` of exactly those units in the incoming order; a unit missing from the payload counts as different |

Decisions and limits:

- Weighted pieces are not hashed: the UI always sends them (0 when unset) and Talabat may omit them in the echo.
- Talabat item status class: `IN_CART` and `ADDITION` count as one class, "present", because the Talabat OpenAPI allows both spellings on request and response and does not say how an added item is echoed. `NOT_FOUND` and `REPLACED` keep their own value and carry no quantity.
- An UPDATE_CART whose items are all `NOT_FOUND` is cancelled by Talabat with reason `ITEM_UNAVAILABLE` (OpenAPI), so the expected state is `CANCELLED` with that reason. Whenever the incoming order is `CANCELLED`, only status and reason are compared.
- Not done: Noon Cancel Shipment. The API says all items of the shipment are cancelled but not which statuses they return with, and the shipment rows are local state, not part of the import. It stays outside the guard until the real post-cancel unit state is known.
- Not covered: Talabat DISPATCHED and READY_FOR_PICKUP, and every Shopify call.

