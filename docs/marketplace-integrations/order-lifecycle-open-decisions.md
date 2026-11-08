# Order lifecycle (#502): open decisions

Status as of 2026-11-09, branch `feat/502-order-lifecycle`. These came out of the review of the order lifecycle work (Edit Order, Cancel Order, nightly reconciliation, Bosta COD sync). Each item was left undone on purpose because it needs an owner decision. Remove an item once decided and implemented.

## Decided 2026-11-09 (recommended defaults accepted)

- **C:** no code change for now; the docs do not cover Delivered after Returned or Failed. Stays open for a later decision.
- **J:** prepaid Shopify orders are cancelled by hand. A refund flow is a separate issue.
- **L:** option (b), keep Needs Attention and Retry.
- **M:** keep "Waiting for Goods" until T1 is decided.
- **K and H:** accepted as built.
- **Carrier Exception row:** hiding it on an unsubmitted picked-up draft (state 47 shows "Picked Up Not Submitted") is intended.

The sections below keep the original reasoning.

## C. Delivered after Returned or Failed

**Question:** should Bosta code 45 (Delivered) move a note's stage back after 46 (Returned), 60 or 48 (Failed)?

**Today:** `integrations/bosta/statuses.py` `may_move_back` allows Delivered only after Lost or Damaged (100, 101). `delivery-note-first.md` says the same. The stage table is marked "PROPOSED, unconfirmed (T9)" and nothing covers Delivered after 46, 60 or 48.

**Effect of leaving it:** if Bosta later reports Delivered, the stage stays Returned or Failed, and the nightly check (`order_lifecycle_reconciliation.py`, the `code == 45` branch) reports the note every night.

**If yes:** extend `may_move_back` and the `code == 45` branch in `_held_by_rank`, add tests, update the docs.

## J. Cancel Order for prepaid Shopify orders

**Question:** how should Cancel Order handle a prepaid Shopify order that has a submitted Payment Entry?

**Today:** `services/order_cancel.py` refuses any submitted Payment Entry that references the order, so prepaid orders cannot use Cancel Order. Decided: do not use ERPNext's "Unlink Advance Payment on Cancellation of Order" setting. No refund doctype or flow exists in this app (`shopify_payments.py`: "No payout, fee or refund handling"). The only candidate is a native Payment Entry (Pay) to the customer, which would be a new accounting flow.

**Needed to proceed:** the paying account, the mode of payment, and whether the refund goes through the Shopify gateway or is recorded only in ERPNext.

**Effect of leaving it:** prepaid orders must be cancelled by hand.

## L. Second COD round after Edit Order adds items

**Question:** how to avoid the second COD round when Edit Order adds an item.

**Today:** an added row is priced from the Item Price and Bosta is sent that total. When Shopify's real price differs, the re-baseline recomputes the COD and a "Bosta COD Differs" row appears that needs a manual Retry. It only happens when Shopify's price differs from the Item Price.

**Options:**
- (a) Queue an automatic `sync_cod` job after the planner commit. No manual step. Recommended if no manual step is wanted.
- (b) Keep the documented Needs Attention and Retry behaviour.
- (c) Warn in the edit dialog when prices differ.

## M. "Waiting for Goods" stage

**Question:** keep or remove the stage?

**Today:** the stage exists (`delivery_note_workflow.py`, workflow JSON) but has no transition into it and nothing writes it. Epic #502, issue #498 and `architecture.md` list it. It is unused only because its driver (T1, `custom_preorder_status` / "Block Delivery Before Goods") is still open.

**Recommendation:** keep it until T1 is decided. Removing it needs a patch.

## K. Nightly check no longer flags under-shipping notes

**Question:** accept the trade-off?

**Today:** `_note_rows_problem` flags only an own-order row that ships more than is left, or a row whose Sales Order row is gone. A note that ships less than the order is not flagged, because it cannot be told apart from a legitimate partial note or a note shared with other orders. Before this change such notes were flagged every night.

## H. Two Bosta COD retry choices

**Question:** confirm both behaviours.

1. `editing.sync_cod` (the Retry) is refused while the order has an edit marker (`outbound_edit_state` set), which covers an unknown Shopify answer.
2. When Shopify accepted the edit but `apply_edit` failed, `edit_order` sets `custom_cod_amount` to the COD Bosta holds, so Retry cannot push the old COD back.
