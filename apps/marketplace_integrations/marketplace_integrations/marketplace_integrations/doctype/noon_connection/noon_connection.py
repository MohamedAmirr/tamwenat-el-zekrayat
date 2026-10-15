# Copyright (c) 2026, Tamwenat El Zekrayat and contributors
# For license information, please see license.txt

import secrets

import frappe
from frappe import _
from frappe.model.document import Document
from frappe.utils import get_url

from marketplace_integrations.integrations.noon.constants import (
	NOON_EVENT_WEBHOOK_PATH,
	NOON_FBPI_ORDER_SYNC_EVENT_TYPE,
	NOON_MAX_DESTINATIONS,
	NOON_WEBHOOK_SECRET_HEADER,
)

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

		ours = self._find_noon_destination(destinations)
		self._store_destinations(destinations, ours)
		status = self._destination_status(ours)
		if ours and not self.noon_destination_code:
			code = ours.get("destination_code")
			if isinstance(code, str) and code:
				self._set_webhook_field("noon_destination_code", code)

		other_count = len(destinations) - (1 if ours else 0)
		return self._cache_webhook_status(status, other_count=other_count)

	@frappe.whitelist()
	def register_webhook(self):
		"""Create our Noon destination, then subscribe it to FBPI order sync."""
		self._check_webhook_write()
		current = self.refresh_webhook_status()
		if current["status"] == "Could not reach Noon":
			frappe.throw(current.get("error") or _("Could not reach Noon."))
		if current["status"] != "Not registered":
			frappe.throw(_("A Noon destination for this connection already exists."))
		if current["other_destinations"] >= NOON_MAX_DESTINATIONS:
			frappe.throw(
				_("Noon allows {0} destinations and this project already has that many.").format(
					NOON_MAX_DESTINATIONS
				)
			)

		client = self._noon_client()
		response = client.create_https_destination(
			f"{self.project_code}-webhook", self.webhook_url, self._webhook_credential_values()
		)
		if response.status_code != 200:
			frappe.throw(
				_("Creating the Noon destination failed: {0}").format(client.error_message(response))
			)
		try:
			code = response.json().get("destination_code")
		except (ValueError, AttributeError):
			code = None
		if not isinstance(code, str) or not code:
			frappe.throw(_("Noon created the destination but returned no destination code. Refresh status."))
		self._set_webhook_field("noon_destination_code", code)

		return self._update_destination_step(
			_("Subscribing the new destination to FBPI order sync"),
			event_types=[{"event_type": NOON_FBPI_ORDER_SYNC_EVENT_TYPE, "is_active": True}],
			created=True,
		)

	@frappe.whitelist()
	def subscribe_webhook(self):
		"""Subscribe our existing Noon destination to FBPI order sync."""
		self._check_webhook_write()
		self._require_destination_code()
		return self._update_destination_step(
			_("Subscribing to FBPI order sync"),
			event_types=[{"event_type": NOON_FBPI_ORDER_SYNC_EVENT_TYPE, "is_active": True}],
		)

	@frappe.whitelist()
	def update_webhook_destination(self, send_key=0):
		"""Push the current URL to Noon; also the key only when ``send_key`` is set."""
		self._check_webhook_write()
		self._require_destination_code()
		fields = {"url": self.webhook_url}
		if frappe.utils.cint(send_key):
			fields["credentials"] = {"values": self._webhook_credential_values()}
		return self._update_destination_step(_("Updating the Noon destination"), **fields)

	@frappe.whitelist()
	def set_webhook_active(self, active):
		"""Activate or deactivate our Noon destination."""
		self._check_webhook_write()
		self._require_destination_code()
		return self._update_destination_step(
			_("Changing the Noon destination's active state"),
			is_active=bool(frappe.utils.cint(active)),
		)

	@frappe.whitelist()
	def set_destination_active(self, destination_code, active):
		"""Activate or deactivate any destination listed by the last status refresh."""
		self._check_webhook_write()
		if destination_code not in {row.destination_code for row in self.noon_destinations}:
			frappe.throw(_("Refresh the webhook status first: that destination is not in the list."))
		return self._update_destination_step(
			_("Changing the Noon destination's active state"),
			destination_code=destination_code,
			is_active=bool(frappe.utils.cint(active)),
		)

	def _check_webhook_write(self):
		self.check_permission("write")
		if self.is_new():
			frappe.throw(_("Save the Noon Connection first."))
		if not self.webhook_url or not self.get_password("webhook_secret", raise_exception=False):
			frappe.throw(_("The webhook URL and key are missing. Save the Noon Connection again."))

	def _require_destination_code(self):
		if not self.noon_destination_code:
			frappe.throw(
				_("Refresh the webhook status first: no Noon destination is known for this connection.")
			)

	def _noon_client(self):
		from marketplace_integrations.integrations.noon.client import NoonClient

		return NoonClient(self.name)

	def _webhook_credential_values(self):
		return {NOON_WEBHOOK_SECRET_HEADER: self.get_password("webhook_secret", raise_exception=False)}

	def _update_destination_step(self, step, *, created=False, destination_code=None, **fields):
		client = self._noon_client()
		response = client.update_destination(destination_code or self.noon_destination_code, **fields)
		if response.status_code != 200:
			prefix = _("The destination was created, but") if created else _("The step failed:")
			frappe.throw(f"{prefix} {step}: {client.error_message(response)}")
		return self.refresh_webhook_status()

	def _store_destinations(self, destinations, ours):
		"""Replace the cached destination rows without a full document save."""
		frappe.db.delete(
			"Noon Destination",
			{"parent": self.name, "parenttype": "Noon Connection", "parentfield": "noon_destinations"},
		)
		self.set("noon_destinations", [])
		for destination in destinations:
			event_types = destination.get("event_types")
			row = self.append(
				"noon_destinations",
				{
					"destination_code": destination.get("destination_code"),
					"destination_name": destination.get("destination_name"),
					"url": destination.get("url"),
					"is_active": 1 if destination.get("is_active") else 0,
					"event_types": ", ".join(event_types) if isinstance(event_types, list) else "",
					"is_ours": 1 if destination is ours else 0,
				},
			)
			row.name = frappe.generate_hash(length=10)
			row.set_user_and_timestamp()
			row.db_insert()

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
		if not isinstance(event_types, list) or NOON_FBPI_ORDER_SYNC_EVENT_TYPE not in event_types:
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
