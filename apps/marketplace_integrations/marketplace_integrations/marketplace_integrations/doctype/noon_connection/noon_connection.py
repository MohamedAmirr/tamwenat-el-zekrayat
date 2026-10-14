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

	@frappe.whitelist()
	def refresh_webhook_status(self):
		"""Fetch and cache this connection's Noon Event Notification status."""
		self.check_permission("write")
		if self.is_new():
			frappe.throw(_("Save the Noon Connection before refreshing webhook status."))

		from marketplace_integrations.integrations.noon.client import NoonClient

		client = NoonClient(self.name)
		try:
			response = client.list_destinations()
			if response.status_code != 200:
				return self._cache_webhook_status(
					"Could not reach Noon", error=client.error_message(response)
				)
			payload = response.json()
		except ValueError, frappe.ValidationError:
			return self._cache_webhook_status("Could not reach Noon")

		destinations = payload.get("destinations") if isinstance(payload, dict) else None
		if not isinstance(destinations, list) or not all(isinstance(row, dict) for row in destinations):
			return self._cache_webhook_status("Could not reach Noon")

		self._set_webhook_field("noon_destinations_debug", frappe.as_json(payload))
		ours = self._find_noon_destination(destinations)
		status = self._destination_status(ours)
		if ours and not self.noon_destination_code:
			code = ours.get("destination_code")
			if isinstance(code, str) and code:
				self._set_webhook_field("noon_destination_code", code)

		other_count = len(destinations) - (1 if ours else 0)
		return self._cache_webhook_status(status, other_count=other_count)

	def _find_noon_destination(self, destinations):
		if self.noon_destination_code:
			return next(
				(
					destination
					for destination in destinations
					if destination.get("destination_code") == self.noon_destination_code
				),
				None,
			)
		return next(
			(destination for destination in destinations if destination.get("url") == self.webhook_url), None
		)

	def _destination_status(self, destination):
		if not destination:
			return "Not registered"
		if destination.get("url") != self.webhook_url:
			return "URL mismatch"
		if not destination.get("is_active"):
			return "Inactive"
		event_types = destination.get("event_types")
		if not isinstance(event_types, list) or "FBPI::ORDER_SYNC" not in event_types:
			return "Registered, not subscribed"
		return "Active"

	def _cache_webhook_status(self, status, *, other_count=0, error=None):
		self._set_webhook_field("webhook_status", status)
		self._set_webhook_field("other_noon_destinations", other_count)
		self._set_webhook_field("last_webhook_event_received_on", self._last_webhook_event_received_on())
		result = {
			"status": status,
			"other_destinations": other_count,
			"last_event_received_on": self.last_webhook_event_received_on,
		}
		if error:
			result["error"] = error
		return result

	def _set_webhook_field(self, fieldname, value):
		setattr(self, fieldname, value)
		self.db_set(fieldname, value, update_modified=False)

	def _last_webhook_event_received_on(self):
		accounts = frappe.get_all(
			"Marketplace Account",
			filters={"platform": "Noon", "connection_doctype": "Noon Connection", "connection": self.name},
			pluck="name",
		)
		if not accounts:
			return None
		return frappe.db.get_value(
			"Marketplace Sync Log",
			{"marketplace_account": ["in", accounts], "trigger_source": "Noon Webhook"},
			"completed_at",
			order_by="completed_at desc",
		)
