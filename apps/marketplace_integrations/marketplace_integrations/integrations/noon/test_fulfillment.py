# Copyright (c) 2026, Tamwenat El Zekrayat and contributors
# For license information, please see license.txt

"""Noon FBPI order-level fulfillment tests (#146 section C, trimmed by #415) - shippable-unit
computation, the available actions, Mark Out of Stock, the order invoice and Sync Log evidence.
Shipment create, cancel and refresh are tested in test_delivery_note_shipment.py. Mocks NoonClient
and never makes real HTTP calls, same convention as integrations/noon/test_orders.py. Fixtures use
only the documented Noon API shapes (``docs/noon-integration/noon_api_full.md``).
"""

import json
from unittest.mock import MagicMock, patch

import frappe
from frappe.tests import IntegrationTestCase
from frappe.utils import nowdate

from marketplace_integrations.integrations.noon import expected_change, fulfillment
from marketplace_integrations.services.marketplace_orders import upsert_marketplace_order

_ADAPTER = "marketplace_integrations.integrations.noon.delivery_note_shipment"
_STARTED = f"{_ADAPTER}.started_units_of_order"
_HAS_CREATED_NOTE = f"{_ADAPTER}.has_created_note"

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


def _delete_noon_account_fixtures(account):
	# delete_doc (not db.delete) so the child rows of Marketplace Order go too
	for order_name in frappe.get_all("Marketplace Order", filters={"marketplace_account": account.name}, pluck="name"):
		frappe.delete_doc("Marketplace Order", order_name, ignore_permissions=True, force=True)
	frappe.db.delete("Marketplace Sync Log", filters={"marketplace_account": account.name})
	frappe.delete_doc("Marketplace Account", account.name, ignore_permissions=True, force=True)
	frappe.delete_doc("Noon Connection", account.connection, ignore_permissions=True, force=True)
	frappe.db.commit()


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

	def test_units_started_by_a_live_noon_delivery_note_are_excluded(self):
		order = _make_fulfillment_order(
			self.account,
			self.fbpi_order_nr,
			[_confirmed_acknowledged(1), _confirmed_acknowledged(2, "SKU-002")],
		)
		with patch(_STARTED, return_value={"1"}) as started:
			shippable = fulfillment.get_shippable_units(order)

		self.assertEqual([row["mp_item_nr"] for row in shippable], ["2"])
		started.assert_called_once_with(order.name)

	def test_units_of_no_live_note_remain_shippable(self):
		order = _make_fulfillment_order(self.account, self.fbpi_order_nr, [_confirmed_acknowledged(1)])
		with patch(_STARTED, return_value=set()):
			shippable = fulfillment.get_shippable_units(order)

		self.assertEqual([row["mp_item_nr"] for row in shippable], ["1"])


class IntegrationTestNoonAvailableActions(IntegrationTestNoonFulfillmentBase):
	def test_acknowledged_order_offers_no_button_action(self):
		# Mark as Out of Stock is an item row button now (#416), not an action of this list
		order = _make_fulfillment_order(self.account, self.fbpi_order_nr, [_confirmed_acknowledged(1)])
		with patch(_HAS_CREATED_NOTE, return_value=False):
			self.assertEqual(
				fulfillment.get_available_actions(order),
				{"actions": [], "cancelled_on_noon": False},
			)

	def test_cancelled_units_offer_nothing_and_flag_cancelled_on_noon(self):
		order = _make_fulfillment_order(
			self.account,
			self.fbpi_order_nr,
			[{**_confirmed_acknowledged(1), "mp_status": "MP_ITEM_STATUS_CANCELLED"}],
		)
		with patch(_HAS_CREATED_NOTE, return_value=False):
			self.assertEqual(
				fulfillment.get_available_actions(order), {"actions": [], "cancelled_on_noon": True}
			)

	def test_the_order_invoice_state_follows_the_created_note_and_the_status(self):
		order = _make_fulfillment_order(self.account, self.fbpi_order_nr, [_confirmed_acknowledged(1)])
		order.noon_order_invoice_file = "/private/files/i.pdf"
		with patch(_HAS_CREATED_NOTE, return_value=True), patch(_STARTED, return_value={"1"}):
			self.assertEqual(
				fulfillment.order_invoice_state(order), {"can_upload": True, "can_retry": False, "uploaded": False}
			)
			self.assertEqual(fulfillment.get_available_actions(order)["actions"], [])
			order.noon_order_invoice_status = "Pending"
			self.assertEqual(
				fulfillment.order_invoice_state(order), {"can_upload": False, "can_retry": True, "uploaded": False}
			)
			order.noon_order_invoice_status = "Error"
			self.assertEqual(
				fulfillment.order_invoice_state(order), {"can_upload": True, "can_retry": True, "uploaded": False}
			)
			order.noon_order_invoice_status = "Uploaded"
			self.assertEqual(
				fulfillment.order_invoice_state(order), {"can_upload": True, "can_retry": False, "uploaded": True}
			)
		with patch(_HAS_CREATED_NOTE, return_value=False):
			self.assertFalse(fulfillment.order_invoice_state(order)["can_upload"])

	def test_pending_oos_reservation_offers_release(self):
		order = _make_fulfillment_order(self.account, self.fbpi_order_nr, [_confirmed_acknowledged(1)])
		order.noon_pending_oos_units = frappe.as_json(["1"])
		order.save()
		with patch(_HAS_CREATED_NOTE, return_value=False):
			self.assertEqual(
				fulfillment.get_available_actions(order)["actions"], ["release_stuck_oos_reservation"]
			)

	def test_endpoint_returns_actions_for_a_noon_order(self):
		from marketplace_integrations.api import noon_fulfillment

		order = _make_fulfillment_order(self.account, self.fbpi_order_nr, [_confirmed_acknowledged(1)])
		with patch(_HAS_CREATED_NOTE, return_value=False):
			self.assertEqual(noon_fulfillment.get_available_actions(order.name)["actions"], [])


class IntegrationTestNoonOrderInvoice(IntegrationTestNoonFulfillmentBase):
	"""UploadFbpiOrderInvoice: the Created-shipment precondition (a Noon Delivery Note), the idempotency
	key, the file rules and the error paths."""

	def setUp(self):
		super().setUp()
		self.order = _make_fulfillment_order(self.account, self.fbpi_order_nr, [_confirmed_acknowledged(1)])
		self._file_content_map = {}
		patcher = patch(_HAS_CREATED_NOTE, return_value=True)
		self.has_created_note = patcher.start()
		self.addCleanup(patcher.stop)
		# the warehouse is FBPI-ready (the refusal is tested below)
		fbpi = patch("marketplace_integrations.integrations.noon.fulfillment.fbpi_ready", return_value=(True, ""))
		self.fbpi_ready = fbpi.start()
		self.addCleanup(fbpi.stop)

	def tearDown(self):
		frappe.db.delete("File", filters={"attached_to_name": self.order.name})
		frappe.db.delete("Integration Request", filters={"custom_operation_type": "Invoice", "reference_docname": self.order.name})
		super().tearDown()

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
		self._file_content_map[file_doc.file_url] = content
		return file_doc

	def _patch_get_content(self):
		"""Serve File content from the test content map (patching File.get_content
		rather than frappe.get_doc, which saving an Attach field also calls)."""
		from frappe.core.doctype.file.file import File

		original_get_content = File.get_content
		content_map = self._file_content_map

		def get_content(file_doc, *args, **kwargs):
			if file_doc.file_url in content_map:
				return content_map[file_doc.file_url]
			return original_get_content(file_doc, *args, **kwargs)

		return patch.object(File, "get_content", autospec=True, side_effect=get_content)

	def _upload(self, file_doc, invoice_nr="INV-1"):
		with (
			self._patch_get_content(),
			patch("marketplace_integrations.integrations.noon.fulfillment.frappe.enqueue") as enqueue,
		):
			enqueue.return_value = MagicMock()
			result = fulfillment.upload_order_invoice(self.order.name, file_doc.file_url, invoice_nr, nowdate())
		self.order.reload()
		return result

	def _run_worker(self, key, status_code=200):
		with (
			self._patch_get_content(),
			patch("marketplace_integrations.integrations.noon.fulfillment.NoonClient") as client_class,
		):
			client_class.return_value.request.return_value = MagicMock(status_code=status_code)
			client_class.return_value.error_message.return_value = "boom"
			fulfillment.run_upload_order_invoice(self.order.name, key)
			self.order.reload()
			return client_class.return_value.request

	def test_requires_a_created_shipment(self):
		self.has_created_note.return_value = False
		file_doc = self._make_file(b"%PDF-1.4\nmock invoice content\n%%EOF")
		with self._patch_get_content(), self.assertRaises(frappe.ValidationError):
			fulfillment.upload_order_invoice(self.order.name, file_doc.file_url, "INV-1", nowdate())
		self.assertFalse(fulfillment.order_invoice_state(self.order)["can_upload"])

	def test_success_sends_order_level_payload_and_marks_uploaded(self):
		file_doc = self._make_file(b"%PDF-1.4\nmock invoice content\n%%EOF")
		result = self._upload(file_doc)
		key = self.order.noon_order_invoice_idempotency_key
		self.assertEqual(result, {"queued": True, "idempotency_key": key})
		self.assertEqual(fulfillment.invoice_result(key)["state"], "pending")
		self.assertEqual(self.order.noon_order_invoice_status, "Pending")

		request = self._run_worker(key)
		args, kwargs = request.call_args
		self.assertEqual(args, ("POST", "/fbpi/v1/fbpi-order/invoice/upload"))
		self.assertEqual(kwargs["json"]["fbpi_order_nr"], self.order.external_order_id)
		self.assertEqual(kwargs["json"]["idempotency_key"], key)
		self.assertEqual(kwargs["json"]["invoice_nr"], "INV-1")
		self.assertEqual(self.order.noon_order_invoice_status, "Uploaded")
		self.assertFalse(self.order.noon_order_invoice_last_error)
		self.assertEqual(fulfillment.invoice_result(key), {"state": "done", "message": ""})
		# the Integration Request is the only log (no Marketplace Sync Log row for the invoice)
		self.assertFalse(
			frappe.db.exists(
				"Marketplace Sync Log", {"marketplace_order": self.order.name, "sync_type": "Fulfillment"}
			)
		)
		# the call is an Integration Request (operation Invoice) on the document: no file bytes, no secrets
		rows = frappe.get_all(
			"Integration Request",
			filters={"custom_operation_type": "Invoice", "reference_doctype": "Marketplace Order", "reference_docname": self.order.name},
			fields=["*"],
		)
		self.assertEqual(len(rows), 1)
		self.assertEqual((rows[0].custom_result, rows[0].custom_correlation_id), ("Success", key))
		self.assertNotIn(kwargs["json"]["invoice"], json.dumps(rows[0], default=str))
		self.assertNotIn("test-key-id", json.dumps(rows[0], default=str))

	def test_the_pdf_becomes_a_private_file_attached_to_the_sales_order_it_was_used_on(self):
		loose = frappe.get_doc(
			{"doctype": "File", "file_name": "loose-invoice.pdf", "content": _MINIMAL_VALID_PDF, "is_private": 1}
		).insert()
		self.addCleanup(frappe.db.delete, "File", {"attached_to_doctype": "Sales Order", "attached_to_name": "SO-TEST-ATTACH"})
		with patch("marketplace_integrations.integrations.noon.fulfillment.frappe.enqueue") as enqueue:
			enqueue.return_value = MagicMock()
			fulfillment.upload_order_invoice(
				self.order.name, loose.file_url, "INV-1", nowdate(), sales_order="SO-TEST-ATTACH"
			)
		self.order.reload()
		attached = frappe.get_all(
			"File",
			filters={"attached_to_doctype": "Sales Order", "attached_to_name": "SO-TEST-ATTACH"},
			fields=["file_url", "is_private"],
		)
		self.assertEqual(len(attached), 1)
		self.assertEqual(attached[0].is_private, 1)
		self.assertEqual(self.order.noon_order_invoice_file, attached[0].file_url)
		self.assertEqual(enqueue.call_args.kwargs["sales_order"], "SO-TEST-ATTACH")

	def test_a_warehouse_that_is_not_fbpi_ready_refuses_the_upload_and_the_retry(self):
		file_doc = self._make_file(b"%PDF-1.4\nmock invoice content\n%%EOF")
		self.fbpi_ready.return_value = (False, "The warehouse is not FBPI.")
		before = self.order.noon_order_invoice_status
		self.assertEqual(before, "Not Uploaded")  # the doctype default: no upload was ever requested
		with self._patch_get_content(), self.assertRaisesRegex(frappe.ValidationError, "not FBPI"):
			fulfillment.upload_order_invoice(self.order.name, file_doc.file_url, "INV-1", nowdate())
		self.order.reload()
		# the refusal comes before any write: the state is untouched and no file was attached
		self.assertEqual(self.order.noon_order_invoice_status, before)
		self.assertFalse(self.order.noon_order_invoice_file)
		self.order.noon_order_invoice_status = "Error"
		self.order.noon_order_invoice_file = file_doc.file_url
		self.order.save()
		with self.assertRaisesRegex(frappe.ValidationError, "not FBPI"):
			fulfillment.retry_order_invoice(self.order.name)

	def test_a_failure_goes_to_the_dialog_result_and_never_blocks_anything(self):
		self._upload(self._make_file(b"%PDF-1.4\nmock invoice content\n%%EOF"))
		key = self.order.noon_order_invoice_idempotency_key
		self._run_worker(key, status_code=500)
		self.assertEqual(fulfillment.invoice_result(key), {"state": "failed", "message": "boom"})
		rows = frappe.get_all(
			"Integration Request",
			filters={"custom_operation_type": "Invoice", "reference_docname": self.order.name},
			fields=["custom_result", "custom_http_status"],
		)
		self.assertEqual([(row.custom_result, int(row.custom_http_status)) for row in rows], [("Unknown", 500)])

	def test_idempotent_retry_reuses_the_same_key(self):
		self._upload(self._make_file(b"%PDF-1.4\nmock invoice content\n%%EOF"))
		key = self.order.noon_order_invoice_idempotency_key
		self._run_worker(key, status_code=500)
		self.assertEqual(self.order.noon_order_invoice_status, "Error")
		self.assertEqual(self.order.noon_order_invoice_last_error, "boom")

		with patch("marketplace_integrations.integrations.noon.fulfillment.frappe.enqueue") as enqueue:
			enqueue.return_value = MagicMock()
			self.assertEqual(
				fulfillment.retry_order_invoice(self.order.name), {"queued": True, "idempotency_key": key}
			)
			self.assertEqual(enqueue.call_args.kwargs["invoice_idempotency_key"], key)
			self.assertEqual(fulfillment.invoice_result(key)["state"], "pending", "Retry starts a new wait")
		request = self._run_worker(key)
		self.assertEqual(request.call_args.kwargs["json"]["idempotency_key"], key)
		self.assertEqual(self.order.noon_order_invoice_status, "Uploaded")
		self.assertFalse(self.order.noon_order_invoice_last_error)

	def test_stale_job_cannot_send_after_a_reupload(self):
		self._upload(self._make_file(b"%PDF-1.4\nfirst\n%%EOF"))
		first_key = self.order.noon_order_invoice_idempotency_key
		self._run_worker(first_key)
		self._upload(self._make_file(b"%PDF-1.4\nsecond\n%%EOF"), "INV-2")
		self.assertNotEqual(self.order.noon_order_invoice_idempotency_key, first_key)
		self.assertEqual(self.order.noon_order_invoice_status, "Pending")
		self.assertFalse(self._run_worker(first_key).called)

	def test_second_upload_is_rejected_while_pending(self):
		self._upload(self._make_file(b"%PDF-1.4\nfirst\n%%EOF"))
		second = self._make_file(b"%PDF-1.4\nsecond\n%%EOF")
		with self._patch_get_content(), self.assertRaises(frappe.ValidationError):
			fulfillment.upload_order_invoice(self.order.name, second.file_url, "INV-2", nowdate())
		self.order.reload()
		self.assertEqual(self.order.noon_order_invoice_nr, "INV-1")

	def test_reupload_over_an_uploaded_order_invoice_is_allowed(self):
		self._upload(self._make_file(b"%PDF-1.4\nfirst\n%%EOF"))
		first_key = self.order.noon_order_invoice_idempotency_key
		self._run_worker(first_key)
		second = self._upload(self._make_file(b"%PDF-1.4\nsecond\n%%EOF"), "INV-2")
		self.assertTrue(second["queued"])
		self.assertNotEqual(self.order.noon_order_invoice_idempotency_key, first_key)
		self.assertEqual(second["idempotency_key"], self.order.noon_order_invoice_idempotency_key)

	def test_rejects_non_pdf_content(self):
		file_doc = self._make_file(b"not a pdf")
		with self._patch_get_content(), self.assertRaises(frappe.ValidationError):
			fulfillment.upload_order_invoice(self.order.name, file_doc.file_url, "INV-1", nowdate())

	def test_rejects_public_file(self):
		file_doc = frappe.get_doc(
			{
				"doctype": "File",
				"file_name": "public-order-invoice-fixture.pdf",
				"content": _MINIMAL_VALID_PDF,
				"is_private": 0,
				"attached_to_doctype": "Marketplace Order",
				"attached_to_name": self.order.name,
			}
		).insert()
		with self.assertRaises(frappe.ValidationError):
			fulfillment.upload_order_invoice(self.order.name, file_doc.file_url, "INV-1", nowdate())

	def test_rejects_oversized_file(self):
		file_doc = self._make_file(b"%PDF-1.4\nmock invoice content exceeding the patched limit\n%%EOF")
		with (
			self._patch_get_content(),
			patch("marketplace_integrations.integrations.noon.fulfillment.SHIPMENT_INVOICE_MAX_BYTES", 10),
			self.assertRaises(frappe.ValidationError),
		):
			fulfillment.upload_order_invoice(self.order.name, file_doc.file_url, "INV-1", nowdate())

	def test_worker_errors_when_the_created_shipment_is_gone(self):
		self._upload(self._make_file(b"%PDF-1.4\nmock invoice content\n%%EOF"))
		key = self.order.noon_order_invoice_idempotency_key
		self.has_created_note.return_value = False
		self.assertFalse(self._run_worker(key).called)
		self.assertEqual(self.order.noon_order_invoice_status, "Error")


class IntegrationTestNoonReadableNames(IntegrationTestNoonFulfillmentBase):
	"""Item names come only from the product mapping (Noon's payload has no title); an unmapped
	SKU shows as the SKU alone."""

	def setUp(self):
		super().setUp()
		self.order = _make_fulfillment_order(
			self.account,
			self.fbpi_order_nr,
			[_confirmed_acknowledged(1), _confirmed_acknowledged(2, "SKU-002"), _confirmed_acknowledged(3)],
		)

	def _mapped(self, mapped_skus):
		"""Patch the mapping lookup: only ``mapped_skus`` (sku -> item_name) resolve."""
		real_get_value = frappe.db.get_value
		resolve = MagicMock(
			side_effect=lambda account, sku: {"item": f"ITEM-{sku}", "mapping_name": "M"}
			if sku in mapped_skus
			else None
		)

		def get_value(doctype, name=None, *args, **kwargs):
			if doctype == "Item" and isinstance(name, str) and name.startswith("ITEM-"):
				return mapped_skus[name[len("ITEM-") :]]
			return real_get_value(doctype, name, *args, **kwargs)

		return (
			patch("marketplace_integrations.integrations.noon.fulfillment.resolve_mapping_and_item", resolve),
			patch.object(frappe.db, "get_value", side_effect=get_value),
			resolve,
		)

	def test_shippable_units_carry_item_name_only_when_mapped_one_lookup_per_sku(self):
		resolve_patch, value_patch, resolve = self._mapped({"SKU-001": "Hand Soap 500ml"})
		with resolve_patch, value_patch:
			units = fulfillment.get_shippable_units(self.order)
		by_nr = {row["mp_item_nr"]: row for row in units}
		self.assertEqual(by_nr["1"]["item_name"], "Hand Soap 500ml")
		self.assertEqual(by_nr["3"]["item_name"], "Hand Soap 500ml")
		self.assertNotIn("item_name", by_nr["2"])
		# SKU-001 is on two units but resolved once.
		self.assertEqual(sorted(call.args[1] for call in resolve.call_args_list), ["SKU-001", "SKU-002"])

	def test_sync_log_adds_the_order_reference_and_keeps_existing_keys(self):
		fulfillment._log_fulfillment(self.order, "UpdateOrder", "Success", {"mp_item_nrs": ["1"]})
		log = frappe.get_last_doc("Marketplace Sync Log", filters={"marketplace_order": self.order.name})
		data = frappe.parse_json(log.request_data)
		self.assertEqual(data["action"], "UpdateOrder")
		self.assertEqual(data["mp_item_nrs"], ["1"])
		self.assertEqual(data["order_reference"], self.order.platform_order_reference or "")
