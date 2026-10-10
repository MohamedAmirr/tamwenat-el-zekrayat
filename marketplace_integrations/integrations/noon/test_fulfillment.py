# Copyright (c) 2026, Tamwenat El Zekrayat and contributors
# For license information, please see license.txt

"""Noon FBPI shipment fulfillment tests (#146 section C) - shippable-unit
computation, shipment allocation/numbering, Create/Cancel/AddAWB/UpdateOrder/
invoice workers, idempotent retry, and Sync Log evidence. Mocks NoonClient
and never makes real HTTP calls, same convention as integrations/noon/
test_orders.py. Fixtures use only the documented Noon API shapes from the
locked plan's facts table (``docs/noon-integration/noon_api_full.md``).
"""

from unittest.mock import MagicMock, patch

import frappe
from frappe.tests import IntegrationTestCase
from frappe.utils import add_to_date, nowdate

from marketplace_integrations.integrations.noon import fulfillment
from marketplace_integrations.services.marketplace_orders import upsert_marketplace_order

# A minimal but structurally valid single-page PDF (proper xref table/startxref
# offset) - unlike the ad-hoc "%PDF-1.4\n...\n%%EOF" fixtures used elsewhere in
# this file, this one is inserted under a ".pdf" filename, which makes Frappe's
# own File.before_insert run pypdf's pdf_contains_js() check; that check raises
# pypdf.errors.PdfReadError on a non-parseable PDF before our own validation
# ever runs, so the fixture content here must actually parse.
_MINIMAL_VALID_PDF = (
	b"%PDF-1.4\n"
	b"1 0 obj<</Type/Catalog/Pages 2 0 R>>endobj\n"
	b"2 0 obj<</Type/Pages/Kids[3 0 R]/Count 1>>endobj\n"
	b"3 0 obj<</Type/Page/Parent 2 0 R/MediaBox[0 0 3 3]>>endobj\n"
	b"xref\n0 4\n0000000000 65535 f \n0000000009 00000 n \n"
	b"0000000052 00000 n \n0000000101 00000 n \n"
	b"trailer<</Size 4/Root 1 0 R>>\nstartxref\n249\n%%EOF"
)


def _make_noon_connection(connection_name, project_code):
	return frappe.get_doc(
		{
			"doctype": "Noon Connection",
			"connection_name": connection_name,
			"project_code": project_code,
			"key_id": "test-key-id",
			"private_key": "-----BEGIN RSA PRIVATE KEY-----\ntest\n-----END RSA PRIVATE KEY-----",
			"environment": "Static Sandbox",
			"enabled": 1,
		}
	).insert()


def _make_noon_account(account_name, warehouse_code="NOON-WH-001", country_code="ae"):
	project_code = f"test-project-{frappe.generate_hash(length=8)}"
	connection = _make_noon_connection(account_name + " Connection", project_code)
	with patch("marketplace_integrations.integrations.noon.catalog.validate_warehouse_code"):
		return frappe.get_doc(
			{
				"doctype": "Marketplace Account",
				"account_name": account_name,
				"platform": "Noon",
				"external_account_id": project_code,
				"connection_doctype": "Noon Connection",
				"connection": connection.name,
				"noon_warehouse_code": warehouse_code,
				"noon_country_code": country_code,
				"enabled": 1,
			}
		).insert()


def _confirmed_acknowledged(mp_item_nr, partner_sku="SKU-001"):
	return {
		"mp_item_nr": mp_item_nr,
		"partner_sku": partner_sku,
		"mp_status": fulfillment.MP_STATUS_CONFIRMED,
		"integration_status": fulfillment.INTEGRATION_STATUS_ACKNOWLEDGED,
	}


def _make_fulfillment_order(account, fbpi_order_nr, payload_items):
	"""Build a Marketplace Order directly via upsert_marketplace_order() with
	a ``latest_payload.items[]`` shaped exactly like a Noon GetFbpiOrder/
	ListFbpiOrders element - this is the only place get_shippable_units()
	reads from (#145 decision), so the Marketplace Order Item child table
	itself is irrelevant to these tests and left empty."""
	return upsert_marketplace_order(
		account.name,
		{
			"external_order_id": fbpi_order_nr,
			"received_on": nowdate(),
			"latest_payload": {"fbpi_order_nr": fbpi_order_nr, "items": payload_items},
			"items": [],
		},
	)


class IntegrationTestNoonFulfillmentBase(IntegrationTestCase):
	def setUp(self):
		suffix = frappe.generate_hash(length=8)
		self.account = _make_noon_account(f"Fulfillment {suffix}")
		self.fbpi_order_nr = f"ORD-FUL-{suffix}"

	def tearDown(self):
		frappe.db.delete("Marketplace Order", filters={"marketplace_account": self.account.name})
		frappe.db.delete("Marketplace Sync Log", filters={"marketplace_account": self.account.name})
		frappe.delete_doc("Marketplace Account", self.account.name, ignore_permissions=True, force=True)
		frappe.delete_doc("Noon Connection", self.account.connection, ignore_permissions=True, force=True)
		frappe.db.commit()


class IntegrationTestNoonShippableUnits(IntegrationTestNoonFulfillmentBase):
	def test_only_confirmed_and_acknowledged_units_are_shippable(self):
		order = _make_fulfillment_order(
			self.account,
			self.fbpi_order_nr,
			[
				_confirmed_acknowledged(1),
				{
					"mp_item_nr": 2,
					"partner_sku": "SKU-002",
					"mp_status": fulfillment.MP_STATUS_CONFIRMED,
					"integration_status": "INTEGRATION_ITEM_STATUS_UNSPECIFIED",
				},
				{
					"mp_item_nr": 3,
					"partner_sku": "SKU-003",
					"mp_status": "MP_ITEM_STATUS_CANCELLED",
					"integration_status": fulfillment.INTEGRATION_STATUS_ACKNOWLEDGED,
				},
			],
		)
		shippable = fulfillment.get_shippable_units(order)
		self.assertEqual([row["mp_item_nr"] for row in shippable], ["1"])

	def test_units_already_claimed_by_an_active_shipment_are_excluded(self):
		order = _make_fulfillment_order(
			self.account,
			self.fbpi_order_nr,
			[_confirmed_acknowledged(1), _confirmed_acknowledged(2, "SKU-002")],
		)
		order.append(
			"noon_shipments",
			{"integration_shipment_nr": f"{self.fbpi_order_nr}-1", "status": "Created", "awb_source": "Noon"},
		)
		order.append(
			"noon_shipment_units",
			{
				"integration_shipment_nr": f"{self.fbpi_order_nr}-1",
				"mp_item_nr": "1",
				"partner_sku": "SKU-001",
			},
		)
		order.save()

		shippable = fulfillment.get_shippable_units(order)
		self.assertEqual([row["mp_item_nr"] for row in shippable], ["2"])

	def test_units_claimed_only_by_a_cancelled_shipment_remain_shippable(self):
		order = _make_fulfillment_order(self.account, self.fbpi_order_nr, [_confirmed_acknowledged(1)])
		order.append(
			"noon_shipments",
			{
				"integration_shipment_nr": f"{self.fbpi_order_nr}-1",
				"status": "Cancelled",
				"awb_source": "Noon",
			},
		)
		order.append(
			"noon_shipment_units",
			{
				"integration_shipment_nr": f"{self.fbpi_order_nr}-1",
				"mp_item_nr": "1",
				"partner_sku": "SKU-001",
			},
		)
		order.save()

		shippable = fulfillment.get_shippable_units(order)
		self.assertEqual([row["mp_item_nr"] for row in shippable], ["1"])


class IntegrationTestNoonAvailableActions(IntegrationTestNoonFulfillmentBase):
	def _add_shipment(self, order, status, **extra):
		order.append(
			"noon_shipments",
			{"integration_shipment_nr": f"{self.fbpi_order_nr}-1", "status": status, "awb_source": "Noon", **extra},
		)
		order.save()

	def test_acknowledged_order_offers_only_unit_actions(self):
		order = _make_fulfillment_order(self.account, self.fbpi_order_nr, [_confirmed_acknowledged(1)])
		self.assertEqual(
			fulfillment.get_available_actions(order),
			{"actions": ["create_shipment", "mark_out_of_stock"], "cancelled_on_noon": False},
		)

	def test_cancelled_units_offer_nothing_and_flag_cancelled_on_noon(self):
		order = _make_fulfillment_order(
			self.account,
			self.fbpi_order_nr,
			[{**_confirmed_acknowledged(1), "mp_status": "MP_ITEM_STATUS_CANCELLED"}],
		)
		self.assertEqual(
			fulfillment.get_available_actions(order), {"actions": [], "cancelled_on_noon": True}
		)

	def test_created_shipment_offers_shipment_actions_and_no_unit_actions(self):
		order = _make_fulfillment_order(self.account, self.fbpi_order_nr, [_confirmed_acknowledged(1)])
		self._add_shipment(order, "Created")
		order.append(
			"noon_shipment_units",
			{"integration_shipment_nr": f"{self.fbpi_order_nr}-1", "mp_item_nr": "1", "partner_sku": "SKU-001"},
		)
		order.save()
		result = fulfillment.get_available_actions(order)
		self.assertEqual(result["actions"], ["cancel_shipment", "add_awb", "upload_invoice"])
		self.assertFalse(result["cancelled_on_noon"])

		# Retry appears once the Created shipment has a Pending invoice upload,
		# and upload_invoice() itself refuses while one is already pending.
		order.noon_shipments[0].invoice_status = "Pending"
		order.save()
		self.assertEqual(
			fulfillment.get_available_actions(order)["actions"], ["cancel_shipment", "add_awb", "retry"]
		)

	def test_error_shipment_offers_retry_and_discard(self):
		order = _make_fulfillment_order(self.account, self.fbpi_order_nr, [_confirmed_acknowledged(1)])
		self._add_shipment(order, "Error", last_error="boom")
		order.append(
			"noon_shipment_units",
			{"integration_shipment_nr": f"{self.fbpi_order_nr}-1", "mp_item_nr": "1", "partner_sku": "SKU-001"},
		)
		order.save()
		self.assertEqual(fulfillment.get_available_actions(order)["actions"], ["retry", "discard_shipment"])

	def test_pending_oos_reservation_offers_release(self):
		order = _make_fulfillment_order(self.account, self.fbpi_order_nr, [_confirmed_acknowledged(1)])
		order.noon_pending_oos_units = frappe.as_json(["1"])
		order.save()
		self.assertEqual(
			fulfillment.get_available_actions(order)["actions"], ["release_stuck_oos_reservation"]
		)

	def test_endpoint_returns_actions_for_a_noon_order(self):
		from marketplace_integrations.api import noon_fulfillment

		order = _make_fulfillment_order(self.account, self.fbpi_order_nr, [_confirmed_acknowledged(1)])
		self.assertEqual(
			noon_fulfillment.get_available_actions(order.name)["actions"],
			["create_shipment", "mark_out_of_stock"],
		)


class IntegrationTestNoonCreateShipmentRequestPath(IntegrationTestNoonFulfillmentBase):
	def setUp(self):
		super().setUp()
		self.order = _make_fulfillment_order(
			self.account,
			self.fbpi_order_nr,
			[_confirmed_acknowledged(1), _confirmed_acknowledged(2, "SKU-002")],
		)

	def test_rejects_units_that_are_not_shippable(self):
		with self.assertRaises(frappe.ValidationError):
			fulfillment.create_shipment(self.order.name, ["999"], "Noon")

	def test_rejects_own_courier_without_courier_or_awb(self):
		with self.assertRaises(frappe.ValidationError):
			fulfillment.create_shipment(self.order.name, ["1"], "Own Courier")

	def test_writes_pending_rows_and_enqueues_without_any_noon_http_call(self):
		with (
			patch("marketplace_integrations.integrations.noon.fulfillment.frappe.enqueue") as enqueue,
			patch("marketplace_integrations.integrations.noon.fulfillment.NoonClient") as client_class,
		):
			enqueue.return_value = MagicMock()
			result = fulfillment.create_shipment(self.order.name, ["1"], "Noon")
			self.assertNotIn("enqueue_after_commit", enqueue.call_args.kwargs)

			client_class.return_value.request.assert_not_called()
		self.assertEqual(result["integration_shipment_nr"], f"{self.fbpi_order_nr}-1")
		self.assertTrue(result["queued"])

		self.order.reload()
		self.assertEqual(len(self.order.noon_shipments), 1)
		self.assertEqual(self.order.noon_shipments[0].status, "Pending")
		self.assertEqual(self.order.noon_shipments[0].awb_source, "Noon")
		self.assertEqual([row.mp_item_nr for row in self.order.noon_shipment_units], ["1"])

	def test_new_shipment_row_creation_is_its_own_not_the_orders_import_time(self):
		frappe.db.set_value("Marketplace Order", self.order.name, "creation", add_to_date(None, hours=-5))
		self.addCleanup(
			frappe.delete_doc, "Marketplace Order", self.order.name, force=True, ignore_permissions=True
		)
		with patch("marketplace_integrations.integrations.noon.fulfillment.frappe.enqueue") as enqueue:
			enqueue.return_value = MagicMock()
			fulfillment.create_shipment(self.order.name, ["1"], "Noon")

		shipment_creation = frappe.db.get_value(
			"Marketplace Order Shipment", {"parent": self.order.name}, "creation"
		)
		self.assertGreater(shipment_creation, add_to_date(None, hours=-1))

	def test_own_courier_writes_a_pending_awb_row(self):
		with patch("marketplace_integrations.integrations.noon.fulfillment.frappe.enqueue") as enqueue:
			enqueue.return_value = MagicMock()
			fulfillment.create_shipment(
				self.order.name, ["1"], "Own Courier", courier="Aramex", awb_nr="AWB-1"
			)

		self.order.reload()
		self.assertEqual(len(self.order.noon_shipment_awbs), 1)
		self.assertEqual(self.order.noon_shipment_awbs[0].courier, "Aramex")
		self.assertEqual(self.order.noon_shipment_awbs[0].awb_nr, "AWB-1")
		self.assertEqual(self.order.noon_shipment_awbs[0].source, "Own Courier")
		self.assertEqual(self.order.noon_shipment_awbs[0].status, "Pending")

	def test_rejects_a_unit_already_claimed_by_an_active_shipment(self):
		with patch("marketplace_integrations.integrations.noon.fulfillment.frappe.enqueue") as enqueue:
			enqueue.return_value = MagicMock()
			fulfillment.create_shipment(self.order.name, ["1"], "Noon")
			with self.assertRaises(frappe.ValidationError):
				fulfillment.create_shipment(self.order.name, ["1"], "Noon")

	def test_shipment_numbers_are_never_reused_after_cancellation(self):
		"""Decision 13: n = 1 + every shipment ever created on this order,
		including cancelled ones."""
		with patch("marketplace_integrations.integrations.noon.fulfillment.frappe.enqueue") as enqueue:
			enqueue.return_value = MagicMock()
			first = fulfillment.create_shipment(self.order.name, ["1"], "Noon")
		self.assertEqual(first["integration_shipment_nr"], f"{self.fbpi_order_nr}-1")

		self.order.reload()
		self.order.noon_shipments[0].status = "Cancelled"
		self.order.save()

		with patch("marketplace_integrations.integrations.noon.fulfillment.frappe.enqueue") as enqueue:
			enqueue.return_value = MagicMock()
			second = fulfillment.create_shipment(self.order.name, ["2"], "Noon")
		self.assertEqual(second["integration_shipment_nr"], f"{self.fbpi_order_nr}-2")


class IntegrationTestNoonCreateShipmentWorker(IntegrationTestNoonFulfillmentBase):
	def setUp(self):
		super().setUp()
		self.order = _make_fulfillment_order(self.account, self.fbpi_order_nr, [_confirmed_acknowledged(1)])
		with patch("marketplace_integrations.integrations.noon.fulfillment.frappe.enqueue") as enqueue:
			enqueue.return_value = MagicMock()
			fulfillment.create_shipment(self.order.name, ["1"], "Noon")
		self.shipment_nr = f"{self.fbpi_order_nr}-1"

	def _patch_refresh(self):
		"""run_create_shipment() always re-fetches+re-imports the order
		first (decision 12) - patched to a no-op returning the same raw
		payload, since re-verifying the re-import path itself is
		integrations/noon/test_orders.py's job, not this module's."""
		return patch(
			"marketplace_integrations.integrations.noon.fulfillment.import_order",
			return_value=(self.order, []),
		), patch(
			"marketplace_integrations.integrations.noon.fulfillment.get_fbpi_order",
			return_value={"fbpi_order_nr": self.fbpi_order_nr, "items": [_confirmed_acknowledged(1)]},
		)

	def test_happy_path_creates_shipment_and_logs_success(self):
		import_patch, get_patch = self._patch_refresh()
		get_response = MagicMock(status_code=404)
		awb_response = MagicMock(status_code=200)
		awb_response.json.return_value = {"awb_nrs": ["NOON-AWB-1"]}
		create_response = MagicMock(status_code=200)
		with (
			import_patch,
			get_patch,
			patch("marketplace_integrations.integrations.noon.fulfillment.NoonClient") as client_class,
		):
			client = client_class.return_value
			client.request.side_effect = [get_response, awb_response, create_response]
			fulfillment.run_create_shipment(self.order.name, self.shipment_nr)

		self.order.reload()
		self.assertEqual(self.order.noon_shipments[0].status, "Created")
		logs = frappe.get_all(
			"Marketplace Sync Log",
			filters={"marketplace_order": self.order.name, "sync_type": "Fulfillment"},
			fields=["status"],
		)
		self.assertEqual([log.status for log in logs], ["Success"])

	def test_noon_400_no_shipment_found_is_treated_as_not_found(self):
		"""Production Noon answers GetShipment for an unknown shipment with HTTP 400
		INVALID_ARGUMENT "No shipment found ..." (not 404); creation must proceed."""
		import_patch, get_patch = self._patch_refresh()
		get_response = MagicMock(status_code=400)
		get_response.json.return_value = {
			"error": {
				"code": "INVALID_ARGUMENT",
				"message": "AssertionError(\"No shipment found with integration_shipment_nr 'X'.\")",
			}
		}
		awb_response = MagicMock(status_code=200)
		awb_response.json.return_value = {"awb_nrs": ["NOON-AWB-1"]}
		create_response = MagicMock(status_code=200)
		with (
			import_patch,
			get_patch,
			patch("marketplace_integrations.integrations.noon.fulfillment.NoonClient") as client_class,
		):
			client = client_class.return_value
			client.request.side_effect = [get_response, awb_response, create_response]
			fulfillment.run_create_shipment(self.order.name, self.shipment_nr)

		self.assertEqual(client.request.call_args.args[1], fulfillment.SHIPMENT_CREATE_PATH)
		self.order.reload()
		self.assertEqual(self.order.noon_shipments[0].status, "Created")

	def test_other_get_shipment_400_never_resends_create(self):
		import_patch, get_patch = self._patch_refresh()
		get_response = MagicMock(status_code=400)
		get_response.json.return_value = {"error": {"code": "INVALID_ARGUMENT", "message": "bad warehouse"}}
		with (
			import_patch,
			get_patch,
			patch("marketplace_integrations.integrations.noon.fulfillment.NoonClient") as client_class,
		):
			client = client_class.return_value
			client.request.return_value = get_response
			client.error_message.return_value = "bad warehouse"
			fulfillment.run_create_shipment(self.order.name, self.shipment_nr)

		self.assertEqual(client.request.call_count, 1)
		self.order.reload()
		self.assertEqual(self.order.noon_shipments[0].status, "Error")

	def test_retry_finds_shipment_via_get_shipment_and_never_resends_create(self):
		"""Decision 15: CreateShipment documents no idempotency key -
		retrying a Pending/Error shipment calls GetShipment first, and a 200
		there means the shipment already exists - no CreateShipment call."""
		import_patch, get_patch = self._patch_refresh()
		get_response = MagicMock(status_code=200)
		get_response.json.return_value = {"awbs": [], "items": [{"mp_item_nr": 1}]}
		with (
			import_patch,
			get_patch,
			patch("marketplace_integrations.integrations.noon.fulfillment.NoonClient") as client_class,
		):
			client = client_class.return_value
			client.request.return_value = get_response
			fulfillment.run_create_shipment(self.order.name, self.shipment_nr)

		self.assertEqual(client.request.call_count, 1)
		self.assertEqual(client.request.call_args.args[1], fulfillment.SHIPMENT_GET_PATH)
		self.order.reload()
		self.assertEqual(self.order.noon_shipments[0].status, "Created")

	def test_get_shipment_server_error_never_resends_create(self):
		import_patch, get_patch = self._patch_refresh()
		with (
			import_patch,
			get_patch,
			patch("marketplace_integrations.integrations.noon.fulfillment.NoonClient") as client_class,
		):
			client = client_class.return_value
			client.request.return_value = MagicMock(status_code=500)
			client.error_message.return_value = "GetShipment unavailable"
			fulfillment.run_create_shipment(self.order.name, self.shipment_nr)
		self.assertEqual(client.request.call_count, 1)
		self.assertEqual(client.request.call_args.args[1], fulfillment.SHIPMENT_GET_PATH)
		self.order.reload()
		self.assertEqual(self.order.noon_shipments[0].status, "Error")

	def test_noon_awb_is_generated_once_and_reused_on_retry(self):
		"""Decision 14: a Noon-generated AWB is fetched/saved/committed
		exactly once, before CreateShipment, and reused on every retry."""
		import_patch, get_patch = self._patch_refresh()
		not_found = MagicMock(status_code=404)
		awb_response = MagicMock(status_code=200)
		awb_response.json.return_value = {"awb_nrs": ["NOON-AWB-1"]}
		create_response = MagicMock(status_code=200)
		with (
			import_patch,
			get_patch,
			patch("marketplace_integrations.integrations.noon.fulfillment.NoonClient") as client_class,
		):
			client = client_class.return_value
			client.request.side_effect = [not_found, awb_response, create_response]
			fulfillment.run_create_shipment(self.order.name, self.shipment_nr)

		self.order.reload()
		self.assertEqual(len(self.order.noon_shipment_awbs), 1)
		self.assertEqual(self.order.noon_shipment_awbs[0].awb_nr, "NOON-AWB-1")
		self.assertEqual(self.order.noon_shipment_awbs[0].status, "Added")

		# Error-and-retry: AWB row already exists, so NOON_LOGISTICS_AWBS_GET_PATH
		# must not be called a second time.
		self.order.noon_shipments[0].status = "Error"
		self.order.save()
		import_patch2, get_patch2 = self._patch_refresh()
		not_found2 = MagicMock(status_code=404)
		create_response2 = MagicMock(status_code=200)
		with (
			import_patch2,
			get_patch2,
			patch("marketplace_integrations.integrations.noon.fulfillment.NoonClient") as client_class2,
		):
			client2 = client_class2.return_value
			client2.request.side_effect = [not_found2, create_response2]
			fulfillment.run_create_shipment(self.order.name, self.shipment_nr)

		paths_called = [call.args[1] for call in client2.request.call_args_list]
		self.assertNotIn(fulfillment.NOON_LOGISTICS_AWBS_GET_PATH, paths_called)
		self.order.reload()
		self.assertEqual(len(self.order.noon_shipment_awbs), 1)
		self.assertEqual(self.order.noon_shipment_awbs[0].awb_nr, "NOON-AWB-1")


class IntegrationTestNoonCancelShipment(IntegrationTestNoonFulfillmentBase):
	def setUp(self):
		super().setUp()
		self.order = _make_fulfillment_order(self.account, self.fbpi_order_nr, [_confirmed_acknowledged(1)])

	def test_cancel_only_allowed_for_created_shipments(self):
		self.order.append(
			"noon_shipments",
			{"integration_shipment_nr": f"{self.fbpi_order_nr}-1", "status": "Pending", "awb_source": "Noon"},
		)
		self.order.save()
		with self.assertRaises(frappe.ValidationError):
			fulfillment.cancel_shipment(self.order.name, f"{self.fbpi_order_nr}-1")

	def test_cancel_sets_cancel_pending_then_worker_marks_cancelled(self):
		self.order.append(
			"noon_shipments",
			{"integration_shipment_nr": f"{self.fbpi_order_nr}-1", "status": "Created", "awb_source": "Noon"},
		)
		self.order.save()

		with patch("marketplace_integrations.integrations.noon.fulfillment.frappe.enqueue") as enqueue:
			enqueue.return_value = MagicMock()
			fulfillment.cancel_shipment(self.order.name, f"{self.fbpi_order_nr}-1")
			self.assertNotIn("enqueue_after_commit", enqueue.call_args.kwargs)
		self.order.reload()
		self.assertEqual(self.order.noon_shipments[0].status, "Cancel Pending")

		with patch("marketplace_integrations.integrations.noon.fulfillment.NoonClient") as client_class:
			client_class.return_value.request.return_value = MagicMock(status_code=200)
			fulfillment.run_cancel_shipment(self.order.name, f"{self.fbpi_order_nr}-1")

		self.order.reload()
		self.assertEqual(self.order.noon_shipments[0].status, "Cancelled")
		logs = frappe.get_all(
			"Marketplace Sync Log",
			filters={"marketplace_order": self.order.name, "sync_type": "Fulfillment"},
			fields=["status"],
		)
		self.assertEqual([log.status for log in logs], ["Success"])


class IntegrationTestNoonDiscardShipment(IntegrationTestNoonFulfillmentBase):
	"""#146 round-3 review finding #2: discard_shipment()/
	release_stuck_oos_reservation() recovery actions."""

	def setUp(self):
		super().setUp()
		self.order = _make_fulfillment_order(self.account, self.fbpi_order_nr, [_confirmed_acknowledged(1)])

	def test_discard_only_allowed_from_error_or_cancel_pending(self):
		self.order.append(
			"noon_shipments",
			{"integration_shipment_nr": f"{self.fbpi_order_nr}-1", "status": "Created", "awb_source": "Noon"},
		)
		self.order.save()
		with self.assertRaises(frappe.ValidationError):
			fulfillment.discard_shipment(self.order.name, f"{self.fbpi_order_nr}-1")

	def test_discard_from_error_releases_claimed_unit(self):
		self.order.append(
			"noon_shipments",
			{
				"integration_shipment_nr": f"{self.fbpi_order_nr}-1",
				"status": "Error",
				"awb_source": "Noon",
				"last_error": "Something went wrong",
			},
		)
		self.order.append(
			"noon_shipment_units",
			{
				"integration_shipment_nr": f"{self.fbpi_order_nr}-1",
				"mp_item_nr": "1",
				"partner_sku": "SKU-001",
			},
		)
		self.order.save()

		result = fulfillment.discard_shipment(self.order.name, f"{self.fbpi_order_nr}-1")
		self.assertTrue(result["discarded"])

		self.order.reload()
		self.assertEqual(self.order.noon_shipments[0].status, "Discarded")
		self.assertIsNone(self.order.noon_shipments[0].last_error)
		# The unit discarding this shipment held is shippable again.
		shippable = {row["mp_item_nr"] for row in fulfillment.get_shippable_units(self.order)}
		self.assertIn("1", shippable)

		logs = frappe.get_all(
			"Marketplace Sync Log",
			filters={"marketplace_order": self.order.name, "sync_type": "Fulfillment"},
			fields=["status"],
		)
		self.assertEqual([log.status for log in logs], ["Success"])

	def test_discard_from_cancel_pending(self):
		self.order.append(
			"noon_shipments",
			{
				"integration_shipment_nr": f"{self.fbpi_order_nr}-1",
				"status": "Cancel Pending",
				"awb_source": "Noon",
			},
		)
		self.order.save()
		fulfillment.discard_shipment(self.order.name, f"{self.fbpi_order_nr}-1")
		self.order.reload()
		self.assertEqual(self.order.noon_shipments[0].status, "Discarded")

	def test_release_stuck_oos_reservation_clears_pending_units(self):
		self.order.noon_pending_oos_units = frappe.as_json(["1"])
		self.order.save()

		result = fulfillment.release_stuck_oos_reservation(self.order.name)
		self.assertEqual(result["released"], ["1"])

		self.order.reload()
		self.assertEqual(frappe.parse_json(self.order.noon_pending_oos_units), [])

	def test_release_stuck_oos_reservation_requires_a_reservation(self):
		with self.assertRaises(frappe.ValidationError):
			fulfillment.release_stuck_oos_reservation(self.order.name)


class IntegrationTestNoonAddAwb(IntegrationTestNoonFulfillmentBase):
	def setUp(self):
		super().setUp()
		self.order = _make_fulfillment_order(self.account, self.fbpi_order_nr, [_confirmed_acknowledged(1)])
		self.shipment_nr = f"{self.fbpi_order_nr}-1"
		self.order.append(
			"noon_shipments",
			{"integration_shipment_nr": self.shipment_nr, "status": "Created", "awb_source": "Own Courier"},
		)
		self.order.save()

	def _queue_awb(self):
		with patch("marketplace_integrations.integrations.noon.fulfillment.frappe.enqueue") as enqueue:
			enqueue.return_value = MagicMock()
			fulfillment.add_awb(self.order.name, self.shipment_nr, "Aramex", "AWB-1")
			self.assertNotIn("enqueue_after_commit", enqueue.call_args.kwargs)

	def test_duplicate_awb_is_rejected_without_another_row(self):
		self._queue_awb()
		with self.assertRaises(frappe.ValidationError):
			self._queue_awb()
		self.order.reload()
		self.assertEqual(len(self.order.noon_shipment_awbs), 1)

	def test_get_shipment_server_error_never_adds_awb(self):
		self._queue_awb()
		with patch("marketplace_integrations.integrations.noon.fulfillment.NoonClient") as client_class:
			client = client_class.return_value
			client.request.return_value = MagicMock(status_code=500)
			client.error_message.return_value = "GetShipment unavailable"
			fulfillment.run_add_awb(self.order.name, self.shipment_nr, "AWB-1")
		self.assertEqual(client.request.call_count, 1)
		self.assertEqual(client.request.call_args.args[1], fulfillment.SHIPMENT_GET_PATH)
		self.order.reload()
		self.assertEqual(self.order.noon_shipment_awbs[0].status, "Pending")

	def test_malformed_get_shipment_body_never_adds_awb(self):
		self._queue_awb()
		with patch("marketplace_integrations.integrations.noon.fulfillment.NoonClient") as client_class:
			client = client_class.return_value
			client.request.return_value = MagicMock(status_code=200)
			client.request.return_value.json.return_value = {}
			fulfillment.run_add_awb(self.order.name, self.shipment_nr, "AWB-1")
		self.assertEqual(client.request.call_count, 1)
		self.order.reload()
		self.assertEqual(self.order.noon_shipment_awbs[0].status, "Pending")

	def test_cancelled_shipment_does_not_add_queued_awb(self):
		self._queue_awb()
		self.order.reload()
		self.order.noon_shipments[0].status = "Cancelled"
		self.order.save()
		with patch("marketplace_integrations.integrations.noon.fulfillment.NoonClient") as client_class:
			fulfillment.run_add_awb(self.order.name, self.shipment_nr, "AWB-1")
			client_class.return_value.request.assert_not_called()
		self.order.reload()
		self.assertEqual(self.order.noon_shipment_awbs[0].status, "Skipped")

	def test_awb_retry_reports_an_already_queued_job(self):
		self._queue_awb()
		with patch(
			"marketplace_integrations.integrations.noon.fulfillment.frappe.enqueue", return_value=None
		):
			self.assertEqual(fulfillment.retry(self.order.name, self.shipment_nr), {"queued": False})


class IntegrationTestNoonMarkOutOfStock(IntegrationTestNoonFulfillmentBase):
	def test_rejects_units_that_are_not_shippable(self):
		order = _make_fulfillment_order(self.account, self.fbpi_order_nr, [_confirmed_acknowledged(1)])
		with self.assertRaises(frappe.ValidationError):
			fulfillment.mark_out_of_stock(order.name, ["999"])

	def test_enqueue_failure_releases_the_persisted_reservation(self):
		order = _make_fulfillment_order(self.account, self.fbpi_order_nr, [_confirmed_acknowledged(1)])
		with patch(
			"marketplace_integrations.integrations.noon.fulfillment.frappe.enqueue",
			side_effect=RuntimeError("queue unavailable"),
		):
			with self.assertRaisesRegex(RuntimeError, "queue unavailable"):
				fulfillment.mark_out_of_stock(order.name, ["1"])
		order.reload()
		self.assertEqual(fulfillment._pending_oos_units(order), set())
		self.assertEqual([row["mp_item_nr"] for row in fulfillment.get_shippable_units(order)], ["1"])
		with patch(
			"marketplace_integrations.integrations.noon.fulfillment.frappe.enqueue", return_value=None
		):
			self.assertEqual(fulfillment.mark_out_of_stock(order.name, ["1"]), {"queued": False})
		order.reload()
		self.assertEqual(fulfillment._pending_oos_units(order), set())

	def test_worker_sends_update_order_and_refreshes(self):
		order = _make_fulfillment_order(self.account, self.fbpi_order_nr, [_confirmed_acknowledged(1)])
		with patch("marketplace_integrations.integrations.noon.fulfillment.frappe.enqueue") as enqueue:
			enqueue.return_value = MagicMock()
			fulfillment.mark_out_of_stock(order.name, ["1"])
		order.reload()
		self.assertEqual(fulfillment._pending_oos_units(order), {"1"})
		self.assertEqual(fulfillment.get_shippable_units(order), [])
		with self.assertRaises(frappe.ValidationError):
			fulfillment.create_shipment(order.name, ["1"], "Noon")
		with (
			patch("marketplace_integrations.integrations.noon.fulfillment.NoonClient") as client_class,
			patch(
				"marketplace_integrations.integrations.noon.fulfillment.get_fbpi_order",
				return_value={"fbpi_order_nr": self.fbpi_order_nr, "items": [_confirmed_acknowledged(1)]},
			),
			patch(
				"marketplace_integrations.integrations.noon.fulfillment.import_order",
				return_value=(order, []),
			),
		):
			client_class.return_value.request.return_value = MagicMock(status_code=200)
			fulfillment.run_mark_out_of_stock(order.name, ["1"])
			payload = client_class.return_value.request.call_args.kwargs["json"]
		self.assertEqual(
			payload["items"],
			[{"mp_item_nr": "1", "status": fulfillment.UPDATE_ORDER_ITEM_STATUS_OUT_OF_STOCK}],
		)
		order.reload()
		self.assertEqual(fulfillment._pending_oos_units(order), set())
		logs = frappe.get_all(
			"Marketplace Sync Log",
			filters={"marketplace_order": order.name, "sync_type": "Fulfillment"},
			fields=["status"],
		)
		self.assertEqual([log.status for log in logs], ["Success"])

	def test_worker_releases_reservation_after_noon_error(self):
		order = _make_fulfillment_order(self.account, self.fbpi_order_nr, [_confirmed_acknowledged(1)])
		with patch("marketplace_integrations.integrations.noon.fulfillment.frappe.enqueue") as enqueue:
			enqueue.return_value = MagicMock()
			fulfillment.mark_out_of_stock(order.name, ["1"])
			first_job_id = enqueue.call_args.kwargs["job_id"]
		with patch("marketplace_integrations.integrations.noon.fulfillment.NoonClient") as client_class:
			client_class.return_value.request.return_value = MagicMock(status_code=500)
			client_class.return_value.error_message.return_value = "Noon unavailable"
			fulfillment.run_mark_out_of_stock(order.name, ["1"])
		order.reload()
		self.assertEqual(fulfillment._pending_oos_units(order), set())
		with patch("marketplace_integrations.integrations.noon.fulfillment.frappe.enqueue") as enqueue:
			enqueue.return_value = MagicMock()
			fulfillment.mark_out_of_stock(order.name, ["1"])
			self.assertNotEqual(enqueue.call_args.kwargs["job_id"], first_job_id)

	def test_worker_releases_only_its_own_reserved_units(self):
		order = _make_fulfillment_order(
			self.account,
			self.fbpi_order_nr,
			[_confirmed_acknowledged(1), _confirmed_acknowledged(2, "SKU-002")],
		)
		with patch("marketplace_integrations.integrations.noon.fulfillment.frappe.enqueue") as enqueue:
			enqueue.return_value = MagicMock()
			fulfillment.mark_out_of_stock(order.name, ["1"])
			fulfillment.mark_out_of_stock(order.name, ["2"])
		with patch("marketplace_integrations.integrations.noon.fulfillment.NoonClient") as client_class:
			client_class.return_value.request.return_value = MagicMock(status_code=500)
			client_class.return_value.error_message.return_value = "Noon unavailable"
			fulfillment.run_mark_out_of_stock(order.name, ["1"])
		order.reload()
		self.assertEqual(fulfillment._pending_oos_units(order), {"2"})

	def test_worker_skips_unit_claimed_after_reservation(self):
		order = _make_fulfillment_order(self.account, self.fbpi_order_nr, [_confirmed_acknowledged(1)])
		with patch("marketplace_integrations.integrations.noon.fulfillment.frappe.enqueue") as enqueue:
			enqueue.return_value = MagicMock()
			fulfillment.mark_out_of_stock(order.name, ["1"])
		order.reload()
		order.append(
			"noon_shipments",
			{"integration_shipment_nr": f"{self.fbpi_order_nr}-1", "status": "Created", "awb_source": "Noon"},
		)
		order.append(
			"noon_shipment_units",
			{
				"integration_shipment_nr": f"{self.fbpi_order_nr}-1",
				"mp_item_nr": "1",
				"partner_sku": "SKU-001",
			},
		)
		order.save()
		with patch("marketplace_integrations.integrations.noon.fulfillment.NoonClient") as client_class:
			fulfillment.run_mark_out_of_stock(order.name, ["1"])
			client_class.return_value.request.assert_not_called()
		order.reload()
		self.assertEqual(fulfillment._pending_oos_units(order), set())


class IntegrationTestNoonShipmentInvoice(IntegrationTestNoonFulfillmentBase):
	def setUp(self):
		super().setUp()
		self.order = _make_fulfillment_order(self.account, self.fbpi_order_nr, [_confirmed_acknowledged(1)])
		self.order.append(
			"noon_shipments",
			{"integration_shipment_nr": f"{self.fbpi_order_nr}-1", "status": "Created", "awb_source": "Noon"},
		)
		self.order.save()
		self.shipment_nr = f"{self.fbpi_order_nr}-1"
		self._file_content_map = {}

	def _make_file(self, content):
		file_doc = frappe.get_doc(
			{
				"doctype": "File",
				"file_name": "invoice-fixture.bin",
				"content": content,
				"is_private": 1,
				"attached_to_doctype": "Marketplace Order",
				"attached_to_name": self.order.name,
			}
		).insert()
		# Store content mapping by file_url for mocking
		self._file_content_map[file_doc.file_url] = content
		return file_doc

	def _patch_get_content(self):
		"""Patch frappe.get_doc for File to use our test content map"""
		original_get_doc = frappe.get_doc

		def mock_get_doc(doctype, *args, **kwargs):
			if doctype == "File":
				file_doc = original_get_doc(doctype, *args, **kwargs)
				file_url = file_doc.file_url
				if file_url in self._file_content_map:
					file_doc.get_content = MagicMock(return_value=self._file_content_map[file_url])
				return file_doc
			return original_get_doc(doctype, *args, **kwargs)

		return patch("frappe.get_doc", side_effect=mock_get_doc)

	def tearDown(self):
		frappe.db.delete("File", filters={"attached_to_name": self.order.name})
		super().tearDown()

	def test_rejects_non_pdf_content(self):
		file_doc = self._make_file(b"not a pdf")
		with self._patch_get_content():
			with self.assertRaises(frappe.ValidationError):
				fulfillment.upload_invoice(
					self.order.name, self.shipment_nr, file_doc.file_url, "INV-1", nowdate()
				)

	def test_rejects_public_file(self):
		"""#146 round-3 review finding #9: a public File must be rejected -
		the field's own description says "Private PDF attachment", but
		nothing enforced it before this fix."""
		file_doc = frappe.get_doc(
			{
				"doctype": "File",
				"file_name": "public-invoice-fixture.pdf",
				"content": _MINIMAL_VALID_PDF,
				"is_private": 0,
				"attached_to_doctype": "Marketplace Order",
				"attached_to_name": self.order.name,
			}
		).insert()
		with self.assertRaises(frappe.ValidationError):
			fulfillment.upload_invoice(self.order.name, self.shipment_nr, file_doc.file_url, "INV-1", nowdate())

	def test_rejects_oversized_file(self):
		file_doc = self._make_file(b"%PDF-1.4\nmock invoice content exceeding the patched limit\n%%EOF")
		with (
			self._patch_get_content(),
			patch("marketplace_integrations.integrations.noon.fulfillment.SHIPMENT_INVOICE_MAX_BYTES", 10),
		):
			with self.assertRaises(frappe.ValidationError):
				fulfillment.upload_invoice(
					self.order.name, self.shipment_nr, file_doc.file_url, "INV-1", nowdate()
				)

	def test_idempotency_key_generated_once_and_reused_on_retry(self):
		file_doc = self._make_file(b"%PDF-1.4\nmock invoice content\n%%EOF")
		with (
			self._patch_get_content(),
			patch("marketplace_integrations.integrations.noon.fulfillment.frappe.enqueue") as enqueue,
		):
			enqueue.return_value = MagicMock()
			fulfillment.upload_invoice(
				self.order.name, self.shipment_nr, file_doc.file_url, "INV-1", nowdate()
			)
		self.order.reload()
		first_key = self.order.noon_shipments[0].invoice_idempotency_key
		self.assertTrue(first_key)

		with (
			self._patch_get_content(),
			patch("marketplace_integrations.integrations.noon.fulfillment.NoonClient") as client_class,
		):
			response = MagicMock(status_code=500)
			client_class.return_value.request.return_value = response
			fulfillment.run_upload_invoice(self.order.name, self.shipment_nr, first_key)

		# Retry (run_upload_invoice again) must send the SAME key, never a new one.
		with (
			self._patch_get_content(),
			patch("marketplace_integrations.integrations.noon.fulfillment.NoonClient") as client_class,
		):
			response = MagicMock(status_code=200)
			client_class.return_value.request.return_value = response
			fulfillment.run_upload_invoice(self.order.name, self.shipment_nr, first_key)
			payload = client_class.return_value.request.call_args.kwargs["json"]
		self.assertEqual(payload["idempotency_key"], first_key)
		# The base64 invoice payload must never reach the Sync Log.
		logs = frappe.get_all(
			"Marketplace Sync Log",
			filters={"marketplace_order": self.order.name, "sync_type": "Fulfillment"},
			fields=["request_data", "response_data"],
		)
		for log in logs:
			self.assertNotIn(payload["invoice"], (log.request_data or "") + (log.response_data or ""))

	def test_second_upload_is_rejected_while_first_is_pending(self):
		first_file = self._make_file(b"%PDF-1.4\nfirst invoice\n%%EOF")
		second_file = self._make_file(b"%PDF-1.4\nsecond invoice\n%%EOF")
		with (
			self._patch_get_content(),
			patch("marketplace_integrations.integrations.noon.fulfillment.frappe.enqueue") as enqueue,
		):
			enqueue.return_value = MagicMock()
			fulfillment.upload_invoice(
				self.order.name, self.shipment_nr, first_file.file_url, "INV-1", nowdate()
			)
			self.assertNotIn("enqueue_after_commit", enqueue.call_args.kwargs)
			with self.assertRaises(frappe.ValidationError):
				fulfillment.upload_invoice(
					self.order.name, self.shipment_nr, second_file.file_url, "INV-2", nowdate()
				)
			self.assertEqual(enqueue.call_count, 1)
		self.order.reload()
		self.assertEqual(self.order.noon_shipments[0].invoice_file, first_file.file_url)
		self.assertEqual(self.order.noon_shipments[0].invoice_nr, "INV-1")
		self.assertEqual(self.order.noon_shipments[0].invoice_status, "Pending")

	def test_stale_invoice_job_cannot_send_a_new_upload(self):
		file_doc = self._make_file(b"%PDF-1.4\nmock invoice content\n%%EOF")
		with (
			self._patch_get_content(),
			patch("marketplace_integrations.integrations.noon.fulfillment.frappe.enqueue") as enqueue,
		):
			enqueue.return_value = MagicMock()
			fulfillment.upload_invoice(
				self.order.name, self.shipment_nr, file_doc.file_url, "INV-1", nowdate()
			)
		self.order.reload()
		first_key = self.order.noon_shipments[0].invoice_idempotency_key
		self.order.noon_shipments[0].invoice_status = "Uploaded"
		self.order.save()
		with (
			self._patch_get_content(),
			patch("marketplace_integrations.integrations.noon.fulfillment.frappe.enqueue") as enqueue,
		):
			enqueue.return_value = MagicMock()
			fulfillment.upload_invoice(
				self.order.name, self.shipment_nr, file_doc.file_url, "INV-2", nowdate()
			)
		self.order.reload()
		second_key = self.order.noon_shipments[0].invoice_idempotency_key
		self.assertNotEqual(first_key, second_key)
		with (
			self._patch_get_content(),
			patch("marketplace_integrations.integrations.noon.fulfillment.NoonClient") as client_class,
		):
			fulfillment.run_upload_invoice(self.order.name, self.shipment_nr, first_key)
			client_class.return_value.request.assert_not_called()

	def test_cancelled_shipment_does_not_upload_queued_invoice(self):
		file_doc = self._make_file(b"%PDF-1.4\nmock invoice content\n%%EOF")
		with (
			self._patch_get_content(),
			patch("marketplace_integrations.integrations.noon.fulfillment.frappe.enqueue") as enqueue,
		):
			enqueue.return_value = MagicMock()
			fulfillment.upload_invoice(
				self.order.name, self.shipment_nr, file_doc.file_url, "INV-1", nowdate()
			)
		self.order.reload()
		key = self.order.noon_shipments[0].invoice_idempotency_key
		self.order.noon_shipments[0].status = "Cancelled"
		self.order.save()
		with (
			self._patch_get_content(),
			patch("marketplace_integrations.integrations.noon.fulfillment.NoonClient") as client_class,
		):
			fulfillment.run_upload_invoice(self.order.name, self.shipment_nr, key)
			client_class.return_value.request.assert_not_called()
		self.order.reload()
		self.assertEqual(self.order.noon_shipments[0].invoice_status, "Error")
		with self.assertRaises(frappe.ValidationError):
			fulfillment.retry(self.order.name, self.shipment_nr)

	def test_invoice_retry_reports_an_already_queued_job(self):
		file_doc = self._make_file(b"%PDF-1.4\nmock invoice content\n%%EOF")
		with (
			self._patch_get_content(),
			patch("marketplace_integrations.integrations.noon.fulfillment.frappe.enqueue") as enqueue,
		):
			enqueue.return_value = MagicMock()
			fulfillment.upload_invoice(
				self.order.name, self.shipment_nr, file_doc.file_url, "INV-1", nowdate()
			)
		with patch(
			"marketplace_integrations.integrations.noon.fulfillment.frappe.enqueue", return_value=None
		):
			self.assertEqual(fulfillment.retry(self.order.name, self.shipment_nr), {"queued": False})

	def test_no_accounting_or_stock_documents_are_ever_created(self):
		"""Decision 23: shipments are Noon-side evidence only."""
		doctypes = ("Sales Invoice", "Payment Entry", "GL Entry", "Delivery Note", "Stock Entry")
		before = {doctype: frappe.db.count(doctype) for doctype in doctypes}
		file_doc = self._make_file(b"%PDF-1.4\nmock invoice content\n%%EOF")
		with (
			self._patch_get_content(),
			patch("marketplace_integrations.integrations.noon.fulfillment.frappe.enqueue") as enqueue,
		):
			enqueue.return_value = MagicMock()
			fulfillment.upload_invoice(
				self.order.name, self.shipment_nr, file_doc.file_url, "INV-1", nowdate()
			)
		with (
			self._patch_get_content(),
			patch("marketplace_integrations.integrations.noon.fulfillment.NoonClient") as client_class,
		):
			client_class.return_value.request.return_value = MagicMock(status_code=200)
			self.order.reload()
			fulfillment.run_upload_invoice(
				self.order.name, self.shipment_nr, self.order.noon_shipments[0].invoice_idempotency_key
			)
		self.assertEqual({doctype: frappe.db.count(doctype) for doctype in doctypes}, before)
