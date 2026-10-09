# Copyright (c) 2026, Tamwenat El Zekrayat and contributors
# For license information, please see license.txt

"""One record per accepted Shopify webhook delivery, keyed by Shopify's own
``X-Shopify-Webhook-Id`` (``webhook_id``, DB-level unique). This is the
idempotency mechanism for #104: Shopify's at-least-once delivery guarantee
means the same webhook_id can be delivered more than once, and the unique
index (not a check-then-insert race) is what makes a duplicate delivery a
harmless no-op - see
``integrations/shopify/webhooks.py::process_shopify_webhook``.

Deliberately does not store the webhook payload itself - canonical order
retrieval/ERPNext document creation from webhook payloads is out of scope for
#104 (see docs/marketplace-integrations/architecture.md), so there is nothing
here worth archiving beyond delivery/processing evidence.

``attempt_count`` tracks how many times ``_process_delivery()``
(``integrations/shopify/webhooks.py``) has actually run this row - the
first attempt included, not just retries - and bounds both a Shopify-
initiated redelivery of a non-``Processed`` row and the scheduled
``retry_errored_webhook_deliveries()`` sweep at
``MAX_WEBHOOK_DELIVERY_ATTEMPTS``, so a permanently-failing delivery is not
retried forever.
"""

from frappe.model.document import Document


class ShopifyWebhookDelivery(Document):
	pass
