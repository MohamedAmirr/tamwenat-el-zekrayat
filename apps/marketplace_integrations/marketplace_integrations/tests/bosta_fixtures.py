# Copyright (c) 2026, Tamwenat El Zekrayat and contributors
# For license information, please see license.txt

"""Real-database fixtures for the Bosta tests.

Only the HTTP layer (``client.http_request``) is faked. The helpers commit, because the
code under test commits; ``BostaFixtureMixin.tearDown`` therefore deletes everything the
test created (and the Error Log rows it produced) and commits again.
"""

from types import SimpleNamespace
from unittest.mock import patch

import frappe
from frappe.utils import add_days, now_datetime, nowdate

BOSTA_ROLES = ("Sales Manager", "Stock User", "Sales User")

# Bosta's city list (GET /cities) and directory (GET /cities/getAllDistricts) as the fake
# serves them by default. Cairo is the city the tests use; its code (EG-01) is what Bosta gets.
DEFAULT_CITIES = {
	"success": True,
	"data": {
		"list": [
			{"_id": "city-cairo", "name": "Cairo", "nameAr": "القاهرة"},
			{"_id": "city-giza", "name": "Giza", "nameAr": "الجيزة"},
			{"_id": "city-alex", "name": "Alexandria", "nameAr": "الإسكندرية"},
		]
	},
}
DEFAULT_ALL_DISTRICTS = {
	"success": True,
	"data": {
		"list": [
			{
				"cityId": "city-cairo",
				"cityName": "Cairo",
				"cityOtherName": "القاهرة",
				"cityCode": "EG-01",
				"districts": [{"_id": "d-nasr", "name": "Nasr City", "nameAr": "مدينة نصر"}],
			},
			{
				"cityId": "city-giza",
				"cityName": "Giza",
				"cityOtherName": "الجيزة",
				"cityCode": "EG-25",
				"districts": [],
			},
			{
				"cityId": "city-alex",
				"cityName": "Alexandria",
				"cityOtherName": "الإسكندرية",
				"cityCode": "EG-02",
				"districts": [],
			},
		]
	},
}


class FakeResponse:
	def __init__(self, status_code=200, body=None, text=None):
		self.status_code = status_code
		self._body = body
		self.text = text if text is not None else frappe.as_json(body or {})
		self.content = self.text.encode()

	def json(self):
		if self._body is None:
			raise ValueError("no json")
		return self._body


def created(tracking="TRK-1", delivery_id="DELIVERY-1"):
	return FakeResponse(
		200,
		{"success": True, "data": {"_id": delivery_id, "trackingNumber": tracking, "state": {"code": 10}}},
	)


LOOKUP_DEFAULTS = {"cities": DEFAULT_CITIES, "all_districts": DEFAULT_ALL_DISTRICTS}


class FakeBosta:
	"""Queue of responses (or exceptions to raise) per endpoint; records every call.

	``cities`` and ``all_districts`` answer with ``DEFAULT_CITIES`` / ``DEFAULT_ALL_DISTRICTS``
	once their queue is empty, so a test only queues them to force a failure or a change.
	"""

	def __init__(self):
		self.queue = {
			"create": [],
			"search": [],
			"get": [],
			"cities": [],
			"all_districts": [],
			"terminate": [],
			"update": [],
		}
		self.calls = []

	def __call__(self, method, url, **kwargs):
		if url.endswith("/cities"):
			kind = "cities"
		elif url.endswith("/cities/getAllDistricts"):
			kind = "all_districts"
		elif method == "DELETE":
			kind = "terminate"
		elif method == "PUT":
			kind = "update"
		else:
			kind = "search" if url.endswith("/deliveries/search") else "create" if method == "POST" else "get"
		self.calls.append(
			(kind, url if kind in ("cities", "all_districts", "terminate") else kwargs.get("json"))
		)
		if kind in LOOKUP_DEFAULTS and not self.queue[kind]:
			return FakeResponse(200, LOOKUP_DEFAULTS[kind])
		item = self.queue[kind].pop(0)
		if isinstance(item, Exception):
			raise item
		return item

	def calls_of(self, kind):
		return [payload for call_kind, payload in self.calls if call_kind == kind]

	def sent(self):
		"""Every call except the read-only city list and directory lookups (GET /cities and
		/cities/getAllDistricts). Those run before validation, so "nothing was sent" means
		nothing but these."""
		return [call for call in self.calls if call[0] not in ("cities", "all_districts")]


class BostaFixtureMixin:
	def setUp(self):
		from marketplace_integrations.integrations.bosta import locations

		locations.reset_request_state()
		self.addCleanup(locations.reset_request_state)
		# Start cold: the city list and directory are cached for 24 hours in the shared cache.
		for key in (
			locations.CITIES_CACHE_KEY,
			locations.DIRECTORY_CACHE_KEY,
			locations.LOOKUP_FAILED_CACHE_KEY,
		):
			frappe.cache().delete_value(key)
		self.suffix = frappe.generate_hash(length=8)
		self._created = []
		self._purged = []
		self._error_log_floor = now_datetime()
		self.http = FakeBosta()
		stub = SimpleNamespace(enabled=1, get_password=lambda *a, **k: "api-key")
		patchers = [
			patch(
				"marketplace_integrations.integrations.bosta.client.http_request",
				side_effect=self.http,
			),
			patch(
				"marketplace_integrations.integrations.bosta.client.BostaClient.get_settings",
				return_value=stub,
			),
		]
		for patcher in patchers:
			patcher.start()
			self.addCleanup(patcher.stop)
		self.company = frappe.defaults.get_global_default("company")
		self.customer = self._track(
			frappe.get_doc(
				{
					"doctype": "Customer",
					"customer_name": f"Bosta Test {self.suffix}",
					"customer_group": frappe.db.get_value("Customer Group", {"is_group": 0}, "name"),
					"territory": frappe.db.get_value("Territory", {"is_group": 0}, "name"),
				}
			).insert()
		)
		self.item = self._track(
			frappe.get_doc(
				{
					"doctype": "Item",
					"item_code": f"TEST-BOSTA-{self.suffix}",
					"item_group": "Products",
					"stock_uom": "Nos",
					"is_stock_item": 0,
				}
			).insert()
		)
		frappe.db.commit()

	def tearDown(self):
		frappe.set_user("Administrator")
		frappe.db.rollback()
		for name in frappe.get_all(
			"Bosta Shipment", filters={"receiver_name": self._receiver()}, pluck="name"
		):
			self._purge("Bosta Shipment", name)
		orders = {d.name for d in self._created if d.doctype == "Sales Order"}
		for delivery_note in frappe.get_all(
			"Delivery Note Item", filters={"against_sales_order": ["in", list(orders)]}, pluck="parent"
		):
			self._purge("Delivery Note", delivery_note)
		for doc in reversed(self._created):
			self._purge(doc.doctype, doc.name)
		filters = {"method": ["like", "Bosta%"]}
		if self._error_log_floor:
			filters["creation"] = [">", self._error_log_floor]
		for name in frappe.get_all("Error Log", filters=filters, pluck="name"):
			frappe.delete_doc("Error Log", name, force=True, ignore_permissions=True, delete_permanently=True)
		for doctype, name in self._purged:
			frappe.db.delete("Version", {"ref_doctype": doctype, "docname": name})
			frappe.db.delete("Comment", {"reference_doctype": doctype, "reference_name": name})
		# delete_doc leaves a nameless "Deleted" comment per document type.
		frappe.db.delete(
			"Comment",
			{
				"comment_type": "Deleted",
				"reference_doctype": [
					"in",
					[*{doctype for doctype, _ in self._purged}, "Error Log", "Notification Settings"],
				],
				"creation": [">=", self._error_log_floor],
			},
		)
		frappe.db.commit()

	def _receiver(self):
		return f"Receiver {self.suffix}"

	def _track(self, doc):
		self._created.append(doc)
		return doc

	def _purge(self, doctype, name):
		"""Delete permanently (no Deleted Document row) and drop the Version rows."""
		if not frappe.db.exists(doctype, name):
			return
		self._purged.append((doctype, name))
		if frappe.db.get_value(doctype, name, "docstatus") == 1:
			if doctype == "Sales Order":
				frappe.db.set_value(doctype, name, "status", "To Deliver and Bill")
			frappe.get_doc(doctype, name).cancel()
		frappe.delete_doc(doctype, name, force=True, ignore_permissions=True, delete_permanently=True)

	def make_order(self, rate=100, qty=1, advance_paid=0, submit=True):
		order = frappe.get_doc(
			{
				"doctype": "Sales Order",
				"customer": self.customer.name,
				"company": self.company,
				"delivery_date": add_days(nowdate(), 3),
				"items": [
					{
						"item_code": self.item.name,
						"qty": qty,
						"rate": rate,
						"delivery_date": add_days(nowdate(), 3),
					}
				],
			}
		).insert()
		if submit:
			order.submit()
		self._track(order)
		if advance_paid:
			frappe.db.set_value("Sales Order", order.name, "advance_paid", advance_paid)
		frappe.db.commit()
		return order

	def make_user(self, role, *extra_roles):
		email = f"bosta-{frappe.scrub(role)}-{len(extra_roles)}-{self.suffix}@example.com"
		user = frappe.get_doc(
			{
				"doctype": "User",
				"email": email,
				"first_name": "Bosta Test",
				"send_welcome_email": 0,
				"roles": [{"role": r} for r in (role, *extra_roles)],
			}
		).insert()
		self._track(user)
		frappe.db.commit()
		return email

	def create(self, order, **overrides):
		from marketplace_integrations.integrations.bosta import deliveries

		confirm_handover = overrides.pop("confirm_handover", True)
		args = {
			"receiver_name": self._receiver(),
			"receiver_phone": "01000000000",
			"city_id": "city-cairo",
			"address_first_line": "12 Example Street",
		}
		args.update(overrides)
		result = deliveries.create_bosta_shipment(order.name, **args)
		# Legacy flow tests opt into this convenience; production creation never posts stock.
		if confirm_handover and result.get("tracking_number"):
			deliveries.confirm_physical_handover(result["shipment"])
		return result

	def shipment_for(self, order, **filters):
		name = frappe.db.get_value("Bosta Shipment", {"sales_order": order.name, **filters}, "name")
		return frappe.get_doc("Bosta Shipment", name) if name else None
