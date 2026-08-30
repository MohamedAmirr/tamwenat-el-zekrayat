# Copyright (c) 2026, Tamwenat El Zekrayat and contributors
# For license information, please see license.txt

import frappe
from frappe.model.document import Document

from marketplace_integrations.integrations.talabat.client import get_access_token_cache_key

# Same base URLs as legacy talabat_integration's Talabat Settings
# (talabat_integration/talabat_integration/doctype/talabat_settings/talabat_settings.py)
# - this doctype is a port, not a redesign, and the Talabat Partner API base
# URLs themselves haven't changed.
SANDBOX_BASE_URL = "https://sandbox.partner.deliveryhero.io"
PRODUCTION_BASE_URL = "https://talabat.partner.deliveryhero.io"


class TalabatConnection(Document):
	def on_update(self):
		"""A saved connection may have rotated credentials or a different API
		environment. Clearing its token on every update is safe and prevents a
		stale cached token from being sent to either endpoint."""
		frappe.cache().delete_value(get_access_token_cache_key(self.name))

	def get_base_url(self):
		"""Return the Talabat Partner API base URL for this connection's
		environment. Deliberately just URL selection - HTTP calls themselves
		belong in integrations/talabat/client.py's TalabatClient, not here."""
		if self.environment == "Production":
			return PRODUCTION_BASE_URL
		return SANDBOX_BASE_URL
