# Copyright (c) 2026, Tamwenat El Zekrayat and contributors
# For license information, please see license.txt

import secrets

import frappe
from frappe import _
from frappe.model.document import Document
from frappe.utils import get_url

from marketplace_integrations.integrations.noon.constants import NOON_EVENT_WEBHOOK_PATH

AUTH_BASE_URL = "https://noon-api-gateway.noon.partners"
SANDBOX_API_BASE_URL = "https://noon-sandbox-api-gateway.noon.partners"


class NoonConnection(Document):
	def validate(self):
		self._validate_environment()
		self.set_webhook_url_if_missing()
		self.set_webhook_secret_if_missing()

	def set_webhook_url_if_missing(self):
		if not self.webhook_url:
			# allow_header_override=False so a request Host header never
			# becomes persisted configuration.
			self.webhook_url = get_url(NOON_EVENT_WEBHOOK_PATH, allow_header_override=False)

	def set_webhook_secret_if_missing(self):
		if not self.webhook_secret:
			self.webhook_secret = secrets.token_urlsafe(32)

	@frappe.whitelist()
	def refresh_webhook_url(self):
		"""Persist the current site-derived webhook URL on demand."""
		self.check_permission("write")
		self.webhook_url = get_url(NOON_EVENT_WEBHOOK_PATH, allow_header_override=False)
		self.save()
		return {"webhook_url": self.webhook_url}

	@frappe.whitelist()
	def get_webhook_secret_for_copy(self):
		"""Return the secret only to an editor explicitly requesting a copy."""
		self.check_permission("write")
		return {"webhook_secret": self.get_password("webhook_secret", raise_exception=False)}

	def _validate_environment(self):
		if self.environment not in ("Production", "Static Sandbox"):
			frappe.throw(_("Select a valid Noon environment: Production or Static Sandbox."))

	def get_auth_base_url(self):
		"""Noon service-account login always uses the production identity host."""
		self._validate_environment()
		return AUTH_BASE_URL

	def get_api_base_url(self):
		"""Return the API host selected for this connection's environment."""
		self._validate_environment()
		if self.environment == "Production":
			return AUTH_BASE_URL
		return SANDBOX_API_BASE_URL

	@frappe.whitelist()
	def test_connection(self):
		"""Check saved credentials with Noon's read-only Whoami endpoint."""
		self.check_permission("write")
		if self.is_new():
			frappe.throw(_("Save the Noon Connection before testing it."))

		from marketplace_integrations.integrations.noon.client import NoonClient

		client = NoonClient(self.name)
		response = client.whoami()
		if response.status_code != 200:
			frappe.throw(client.error_message(response))
		return {"connected": True}
