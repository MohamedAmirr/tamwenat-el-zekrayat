# Shopify Reconciliation Dashboard — v1 Exclusions & Security Review (#141)

> **Update (post-review follow-ups):** `get_last_catalog_sweep()` and
> `get_refund_evidence_gaps()` — both reviewed below — were removed as dead
> code (no live caller left). Read the findings below as evidence of the
> access control they had while they existed, not as a description of
> current live surfaces.

## Security/permission review (#139's new surfaces)

Reviewed: the two new whitelisted methods (`get_last_catalog_sweep`,
`get_order_import_failures`/`get_refund_evidence_gaps`), the "Run sweep now" trigger
(`queue_catalog_sweep`, unchanged from #105), and the 5 Number Cards' visibility.

**Findings: no fix required.** Access control is already correctly scoped:

- `get_last_catalog_sweep()` (`integrations/shopify/products.py`) is permission-checked
  the same way as `get_account()`: a per-document `read` check on the loaded
  Marketplace Account plus a doctype-level `read` check on Marketplace Product
  Mapping. It is deliberately **not** owner-scoped (unlike `get_sweep_status()`) — any
  user with read access to that account/mapping doctype can see the account's last
  sweep outcome, which is correct for a shared dashboard.
- `get_order_import_failures()`/`get_refund_evidence_gaps()`
  (`services/shopify_reconciliation.py`) both require `frappe.only_for("System
  Manager")` explicitly, matching `Marketplace Sync Log`'s and `Marketplace Order`'s
  own DocType permissions (`read` restricted to System Manager only, no other role).
- The 5 Number Cards' underlying doctypes (`Marketplace Product Mapping`, `Shopify
  Webhook Delivery`, `Marketplace Order`) are all System-Manager-only for `read` at the
  DocType level already — a non-System-Manager user gets zero data from any of these
  cards regardless of the dashboard's own code, the same as they would from the
  existing list views. The new "Shopify Reconciliation" Workspace fixture additionally
  restricts itself to the System Manager role (`roles` on the Workspace doctype) as a
  belt-and-suspenders UI-level restriction, not because the underlying permission was
  insufficient.
- **Cross-account visibility is unchanged, not newly introduced.** None of
  `Marketplace Product Mapping`/`Marketplace Order`/`Marketplace Sync Log`/`Shopify
  Webhook Delivery` scope `read` by account via User Permissions — any System Manager
  already sees every account's rows in the existing list views today. The dashboard
  surfaces the same data through Number Cards/a Page instead of a list view; it does
  not change who can see what. If per-account access restriction is ever wanted, it
  needs its own issue (User Permission records on Marketplace Account) — out of scope
  here per "don't over-engineer".

## v1 exclusions

This dashboard is a v1 surface for Shopify-only reconciliation. It explicitly does
**not** cover:

- **Section 7 — payout gaps.** Blocked on #111 (payout data isn't imported yet).
- **Talabat reconciliation.** This dashboard is Shopify-only; Talabat has its own
  historical error/webhook surfaces (see `docs/talabat-integration/`), not merged into
  this Workspace.
- **Historical/trend charts.** Every section shows current/live state only — no
  time-series, no "errors over the last 30 days" chart, no Dashboard Chart fixtures.
- **Alerting or email digests** beyond what already exists (#136's refund-evidence
  Notification on `Marketplace Order`). No new scheduled email summary, Slack/webhook
  alert, or digest is part of this dashboard.
- **Automatic remediation.** Every section is read-only reporting plus, at most, a
  manual trigger the operator already had available elsewhere (Run sweep now, the
  existing resync button). Nothing here retries/fixes a failure on its own beyond what
  already runs on a schedule (e.g. the hourly webhook retry sweep, which predates this
  dashboard).
- **Platform-scoping the catalog/inventory Number Cards.** `Marketplace Product
  Mapping` has no `platform` field of its own; the two catalog/inventory cards count
  all mappings in the relevant error state, which is Shopify-only in practice today. If
  a second platform starts writing to this doctype, those cards will need
  platform-scoping — not built ahead of that need.
- **Changing `MAX_WEBHOOK_DELIVERY_ATTEMPTS` or the retry sweep's cadence/logic.**
  The "Exhausted Retries" card observes the existing constant; it does not change it.

Treat this dashboard as an operational error/exception surface for what's already
being tracked — not a complete reconciliation system.
