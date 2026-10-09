# Copyright (c) 2026, Tamwenat El Zekrayat and contributors
# For license information, please see license.txt

"""#136: the declarative Notification fixture (fixtures/notification.json,
exported via hooks.py) that flags refund evidence (#109) on Marketplace
Order. No new doc_events are added for this - Frappe core's own
Notification controller (frappe.model.document.Document.run_notifications)
evaluates "Value Change" alerts synchronously on every save, unconditional
on doc_events - see test_no_catalog_document_events_are_registered in
api/test_talabat_catalog.py, which this module does not touch.

Follows test_orders.py's own pattern: ShopifyClient.execute() is mocked,
never a real Shopify call, and orders.import_order() is driven the same way
a REFUNDS_CREATE (or any order) webhook delivery would drive it via
_handle_topic()."""

from unittest.mock import patch

import frappe
from frappe.tests import IntegrationTestCase

from marketplace_integrations.integrations.shopify import orders
from marketplace_integrations.integrations.shopify.test_orders import make_order_payload
from marketplace_integrations.integrations.shopify.test_products import make_shopify_account

EXECUTE_TARGET = "marketplace_integrations.integrations.shopify.client.ShopifyClient.execute"


def _order_gid(suffix):
	return f"gid://shopify/Order/{suffix}"


def _make_system_manager_user(suffix):
	"""A real (non-Administrator) System Manager user - Role.get_info_based_on_role()
	excludes admin@example.com/guest@example.com and Administrator has no `Has Role` row
	of its own, so the Notification's recipient list is empty without one."""
	return frappe.get_doc(
		{
			"doctype": "User",
			"email": f"refund-notify-{suffix}@example.com",
			"first_name": "Refund Notify Test User",
			"send_welcome_email": 0,
			"roles": [{"role": "System Manager"}],
		}
	).insert(ignore_permissions=True)


# Records registered in setUp; drained by _cleanup_created() in tearDown.
_CREATED = []


def _track(doc):
	_CREATED.append(doc)
	return doc


def _cleanup_created():
	"""Delete tracked Marketplace Accounts with their Orders (and Notification Logs for those
	Orders), Sync Logs and Mappings, then the Shopify Connection. Names are re-checked against
	the DB so fixtures that never got inserted are skipped."""
	accounts = [d.name for d in _CREATED if d.doctype == "Marketplace Account"]
	_CREATED.clear()
	for name in accounts:
		if not frappe.db.exists("Marketplace Account", name):
			continue
		connection = frappe.db.get_value("Marketplace Account", name, "connection")
		# Orders have child tables, so delete them via delete_doc (db.delete would orphan those rows).
		for order in frappe.get_all("Marketplace Order", {"marketplace_account": name}, pluck="name"):
			frappe.db.delete(
				"Notification Log", {"document_type": "Marketplace Order", "document_name": order}
			)
			frappe.delete_doc("Marketplace Order", order, ignore_permissions=True, force=True)
		frappe.db.delete("Marketplace Sync Log", {"marketplace_account": name})
		frappe.db.delete("Marketplace Product Mapping", {"marketplace_account": name})
		frappe.delete_doc("Marketplace Account", name, ignore_permissions=True, force=True)
		if connection and frappe.db.exists("Shopify Connection", connection):
			frappe.delete_doc("Shopify Connection", connection, ignore_permissions=True, force=True)


class IntegrationTestRefundNotification(IntegrationTestCase):
	def setUp(self):
		suffix = frappe.generate_hash(length=8)
		self.account = _track(make_shopify_account(f"Refund Notify Account {suffix}"))
		self.recipient = _make_system_manager_user(suffix)

	def tearDown(self):
		_cleanup_created()

	def _notification_logs(self, order_name):
		return frappe.get_all(
			"Notification Log",
			filters={
				"for_user": self.recipient.name,
				"document_type": "Marketplace Order",
				"document_name": order_name,
			},
		)

	def test_notification_log_created_when_refunds_transitions_from_empty_to_populated(self):
		order_gid = _order_gid(frappe.generate_hash(length=8))
		no_refund_payload = make_order_payload(order_gid, updated_at="2026-09-27T10:05:00Z", refunds=[])
		refund_payload = make_order_payload(order_gid, updated_at="2026-09-27T11:00:00Z")

		with patch(EXECUTE_TARGET, return_value={"order": no_refund_payload}):
			order = orders.import_order(self.account.name, order_gid)
		self.assertEqual(self._notification_logs(order.name), [])

		with patch(EXECUTE_TARGET, return_value={"order": refund_payload}):
			orders.import_order(self.account.name, order_gid)

		self.assertEqual(len(self._notification_logs(order.name)), 1)

	def test_no_duplicate_notification_when_refunds_content_is_unchanged(self):
		order_gid = _order_gid(frappe.generate_hash(length=8))
		no_refund_payload = make_order_payload(order_gid, updated_at="2026-09-27T10:05:00Z", refunds=[])
		refund_payload = make_order_payload(order_gid, updated_at="2026-09-27T11:00:00Z")
		# Same refund content, later updated_at only - simulates a redelivered/retried
		# REFUNDS_CREATE webhook re-fetching the same order, not a second distinct refund.
		unchanged_refund_payload = make_order_payload(order_gid, updated_at="2026-09-27T12:00:00Z")

		with patch(EXECUTE_TARGET, return_value={"order": no_refund_payload}):
			order = orders.import_order(self.account.name, order_gid)
		with patch(EXECUTE_TARGET, return_value={"order": refund_payload}):
			orders.import_order(self.account.name, order_gid)
		self.assertEqual(len(self._notification_logs(order.name)), 1)

		with patch(EXECUTE_TARGET, return_value={"order": unchanged_refund_payload}):
			orders.import_order(self.account.name, order_gid)

		self.assertEqual(len(self._notification_logs(order.name)), 1)

	def test_notification_message_identifies_the_order_and_links_to_it(self):
		order_gid = _order_gid(frappe.generate_hash(length=8))
		no_refund_payload = make_order_payload(order_gid, updated_at="2026-09-27T10:05:00Z", refunds=[])
		refund_payload = make_order_payload(order_gid, updated_at="2026-09-27T11:00:00Z")

		with patch(EXECUTE_TARGET, return_value={"order": no_refund_payload}):
			order = orders.import_order(self.account.name, order_gid)
		with patch(EXECUTE_TARGET, return_value={"order": refund_payload}):
			orders.import_order(self.account.name, order_gid)

		log = frappe.get_doc("Notification Log", self._notification_logs(order.name)[0].name)
		self.assertIn(order.order_code, log.subject)
		self.assertIn(f"/app/marketplace-order/{order.name}", log.description)
