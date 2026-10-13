# Copyright (c) 2026, Tamwenat El Zekrayat and contributors
# For license information, please see license.txt

import secrets

import frappe
from frappe.model.document import Document
from frappe.utils import get_url

from marketplace_integrations.integrations.bosta.constants import BOSTA_WEBHOOK_PATH


class BostaSettings(Document):
	"""Single, carrier-specific Bosta configuration for this ERPNext site."""

	def validate(self):
		self.set_webhook_url_if_missing()
		self.set_webhook_secret_if_missing()
		if self.enabled:
			self._validate_enabled_configuration()

	def set_webhook_url_if_missing(self):
		if not self.webhook_url:
			self.webhook_url = self.get_webhook_url()

	def set_webhook_secret_if_missing(self):
		if not self.webhook_secret:
			self.webhook_secret = secrets.token_urlsafe(32)

	def get_webhook_url(self):
		"""Build a stable public URL without trusting the request Host header."""
		return get_url(BOSTA_WEBHOOK_PATH, allow_header_override=False)

	def _validate_enabled_configuration(self):
		if not self.get_password("api_key", raise_exception=False):
			frappe.throw("Bosta Settings requires an API key when enabled.")
		if not self.business_location_id:
			frappe.throw("Bosta Settings requires a business location ID when enabled.")

	@frappe.whitelist()
	def refresh_webhook_url(self):
		"""Persist the current site-derived webhook URL on administrator request."""
		self.check_permission("write")
		self.webhook_url = self.get_webhook_url()
		self.save()
		return {"webhook_url": self.webhook_url}

	@frappe.whitelist()
	def get_webhook_secret_for_copy(self):
		"""Return the secret only after an editor explicitly requests it."""
		self.check_permission("write")
		return {"webhook_secret": self.get_password("webhook_secret", raise_exception=False)}
