# Marketplace Order sync status

How `Marketplace Order.erpnext_sync_status` is decided, which cases end in which status, and which of them the app resolves by itself. The implementation is in `services/sync_reason_codes.py`, `services/sales_order_reconciliation.py` (`plan()`), `services/sales_order_sync.py`, `services/sales_order_sync_debounce.py` and `services/sales_order_recheck.py`; the shared architecture is in [`architecture.md`](architecture.md). The case numbers 1-20 below are the inventory of what `plan()` and the sync service can report for a Sales Order that is already submitted.

## Status model

| Status | Meaning | Expected behavior |
|---|---|---|
| Syncing | A marketplace or ERPNext change is known and processing is pending or running, including a temporary retry. A new import starts here. | The sync runs after the 45 second debounce. Transient failures are retried (see below). |
| Synced | ERPNext matches the intended marketplace state, or an approved reconciliation has been completed. | No staff action. |
| Sync Error | The integration cannot reliably build, interpret or execute the sync: invalid or missing integration data, or a technical failure. | Shows the concrete error. Recoverable problems are retried or re-synced automatically. |
| Needs Action | The desired change is understood, but applying it needs a human operational or accounting decision, approval, physical verification, or manual correction of conflicting ERP documents. | Shows the required action and any draft the app prepared. |
| Cancelled | Terminal: the order was cancelled before any Sales Order existed (#353). | Excluded from every sweep and catch-up. |

"Not Synced" and "Modified After Sync" no longer exist. Sales Orders stay auto-submitted at import. The 45 second debounce is unchanged and covers webhooks, polls, sweeps and form edits.

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

**Receipt gate.** A marketplace `return`, `refund` or `cancelled` event does not prove goods were received, and neither does a Bosta status. The order waits in Needs Action with action state Awaiting Receipt. A user with the Stock Manager or System Manager role presses **Confirm Goods Received** on the Marketplace Order (the whitelisted method checks the role on the server and records `receipt_confirmed_by` and `receipt_confirmed_on`). The app then:

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

- Document events: a Delivery Note, Sales Invoice or Payment Entry submit, cancel or delete, or a Sales Order update after submit or cancel, queues a re-check of the linked orders that are in Needs Action or Sync Error (matched through the Sales Order link or as the recorded blocking or prepared document). The order shows Recheck Queued and the trigger source is "ERPNext Document Change". The handler only bumps the debounce: it never raises, makes no HTTP call and ignores the order whose sync is running.
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
