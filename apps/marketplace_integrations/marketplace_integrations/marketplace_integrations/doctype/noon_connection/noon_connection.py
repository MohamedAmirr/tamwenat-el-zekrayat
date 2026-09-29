# Copyright (c) 2026, Tamwenat El Zekrayat and contributors
# For license information, please see license.txt

import frappe
from frappe import _
from frappe.model.document import Document

AUTH_BASE_URL = "https://noon-api-gateway.noon.partners"
SANDBOX_API_BASE_URL = "https://noon-sandbox-api-gateway.noon.partners"


class NoonConnection(Document):
	def validate(self):
		self._validate_environment()

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
