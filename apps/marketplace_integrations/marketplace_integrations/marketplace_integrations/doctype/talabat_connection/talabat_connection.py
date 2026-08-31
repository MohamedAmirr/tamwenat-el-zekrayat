# Copyright (c) 2026, Tamwenat El Zekrayat and contributors
# For license information, please see license.txt

import frappe
from frappe import _
from frappe.model.document import Document
from frappe.utils import get_url

from marketplace_integrations.integrations.talabat.client import get_access_token_cache_key

# Same base URLs as legacy talabat_integration's Talabat Settings
# (talabat_integration/talabat_integration/doctype/talabat_settings/talabat_settings.py)
# - this doctype is a port, not a redesign, and the Talabat Partner API base
# URLs themselves haven't changed.
SANDBOX_BASE_URL = "https://sandbox.partner.deliveryhero.io"
PRODUCTION_BASE_URL = "https://talabat.partner.deliveryhero.io"


class TalabatConnection(Document):
	def before_insert(self):
		self.set_order_webhook_url_if_missing()

	def on_update(self):
		"""A saved connection may have rotated credentials or a different API
		environment. Clearing its token on every update is safe and prevents a
		stale cached token from being sent to either endpoint."""
		frappe.cache().delete_value(get_access_token_cache_key(self.name))

	def set_order_webhook_url_if_missing(self):
		if not self.order_webhook_url:
			self.order_webhook_url = self.get_order_webhook_url()

	def get_order_webhook_url(self):
		"""Build the public order webhook endpoint from the configured site URL.

		``allow_header_override=False`` prevents a request Host header from
		becoming persisted configuration. Talabat itself must be updated in its
		portal after an administrator explicitly refreshes this URL.
		"""
		return get_url(
			"/api/method/marketplace_integrations.api.talabat_webhooks.order_event",
			allow_header_override=False,
		)

	@frappe.whitelist()
	def refresh_order_webhook_url(self):
		"""Persist the current site-derived order webhook URL on demand."""
		self.check_permission("write")
		self.order_webhook_url = self.get_order_webhook_url()
		self.save()
		return {"order_webhook_url": self.order_webhook_url}

	def get_base_url(self):
		"""Return the Talabat Partner API base URL for this connection's
		environment. Deliberately just URL selection - HTTP calls themselves
		belong in integrations/talabat/client.py's TalabatClient, not here."""
		if self.environment == "Production":
			return PRODUCTION_BASE_URL
		return SANDBOX_BASE_URL
