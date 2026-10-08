# Copyright (c) 2026, Tamwenat El Zekrayat and contributors
# For license information, please see license.txt

"""Noon FBPI return reference lookup/evidence tests (#153). Mocks NoonClient
and never makes real HTTP calls, same convention as test_fulfillment.py.
Covers: dedupe-not-duplicate on a re-lookup, unlinked-never-force-attached
order linking, AWB scan reference matching, empty-items Success log with no
record, non-2xx/transport-error Error log with no record, and the absence
of any ERPNext side effect (Credit Note/Invoice/Payment Entry/GL/stock
ledger/Sales Order).
"""

from unittest.mock import MagicMock, patch

import frappe
from frappe.tests import IntegrationTestCase
from frappe.utils import now_datetime

from marketplace_integrations.integrations.noon import returns
from marketplace_integrations.integrations.noon.constants import AWB_SCAN_LOOKBACK_DAYS
from marketplace_integrations.services.marketplace_orders import upsert_marketplace_order


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


def _make_order(account, fbpi_order_nr, mp_order_nr=None):
	return upsert_marketplace_order(
		account.name,
		{
			"external_order_id": fbpi_order_nr,
			"platform_order_reference": mp_order_nr or fbpi_order_nr,
			"latest_payload": {"fbpi_order_nr": fbpi_order_nr, "items": []},
			"items": [],
		},
	)


def _return_item(purchase_item_nr="PI-1", order_nr=None, references=None):
	item = {
		"purchase_item_nr": purchase_item_nr,
		"partner_sku": "SKU-001",
		"mp_code": "noon",
		"merchant_code": "MC-1",
		"references": references if references is not None else [],
	}
	if order_nr is not None:
		item["order_nr"] = order_nr
	return item


class IntegrationTestNoonReturnsBase(IntegrationTestCase):
	def setUp(self):
		suffix = frappe.generate_hash(length=8)
		self.account = _make_noon_account(f"Returns {suffix}")

	def tearDown(self):
		frappe.db.delete("Noon Return", filters={"marketplace_account": self.account.name})
		frappe.db.delete("Marketplace Order", filters={"marketplace_account": self.account.name})
		frappe.db.delete("Marketplace Sync Log", filters={"marketplace_account": self.account.name})
		frappe.delete_doc("Marketplace Account", self.account.name, ignore_permissions=True, force=True)
		frappe.delete_doc("Noon Connection", self.account.connection, ignore_permissions=True, force=True)
		frappe.db.commit()

	def _mock_response(self, status_code=200, json_body=None):
		response = MagicMock()
		response.status_code = status_code
		response.json.return_value = json_body if json_body is not None else {"items": []}
		return response


class IntegrationTestUpsertReturnItem(IntegrationTestNoonReturnsBase):
	def test_relookup_updates_not_duplicates(self):
		"""Same purchase_item_nr looked up twice updates one record, not a
		duplicate (#153 dedupe key)."""
		item = _return_item(purchase_item_nr="PI-DEDUPE")
		first = returns.upsert_return_item(self.account, item, "Manual Lookup")
		second = returns.upsert_return_item(self.account, item, "Manual Lookup")

		self.assertEqual(first.name, second.name)
		count = frappe.db.count(
			"Noon Return",
			filters={"marketplace_account": self.account.name, "purchase_item_nr": "PI-DEDUPE"},
		)
		self.assertEqual(count, 1)

	def test_relookup_updates_last_seen_on(self):
		item = _return_item(purchase_item_nr="PI-SEEN")
		first = returns.upsert_return_item(self.account, item, "Manual Lookup")
		first_seen = first.first_seen_on
		second = returns.upsert_return_item(self.account, item, "Manual Lookup")
		self.assertEqual(second.first_seen_on, first_seen)
		self.assertIsNotNone(second.last_seen_on)

	def test_unmatched_order_nr_stored_unlinked(self):
		"""A return with no exact order_nr match is stored unlinked, never
		force-attached."""
		item = _return_item(purchase_item_nr="PI-UNLINKED", order_nr="NO-SUCH-ORDER")
		doc = returns.upsert_return_item(self.account, item, "Manual Lookup")
		self.assertFalse(doc.marketplace_order)

	def test_matched_order_nr_links_on_external_order_id(self):
		order = _make_order(self.account, "FBPI-100")
		item = _return_item(purchase_item_nr="PI-LINKED", order_nr="FBPI-100")
		doc = returns.upsert_return_item(self.account, item, "Manual Lookup")
		self.assertEqual(doc.marketplace_order, order.name)

	def test_matched_order_nr_links_on_platform_order_reference(self):
		order = _make_order(self.account, "FBPI-200", mp_order_nr="MP-200")
		item = _return_item(purchase_item_nr="PI-LINKED-2", order_nr="MP-200")
		doc = returns.upsert_return_item(self.account, item, "Manual Lookup")
		self.assertEqual(doc.marketplace_order, order.name)

	def test_malformed_item_without_purchase_item_nr_is_skipped(self):
		doc = returns.upsert_return_item(self.account, {}, "Manual Lookup")
		self.assertIsNone(doc)


class IntegrationTestUpsertReturnItemBumpsSync(IntegrationTestNoonReturnsBase):
	"""#164: a return newly linked to an order that has a Sales Order re-queues its sync."""

	def _debounce(self, order):
		return frappe.db.get_value(
			"Marketplace Order",
			order.name,
			["sync_debounce_until", "pending_sync_trigger_source"],
			as_dict=True,
		)

	def _link_sales_order(self, order):
		# Plain DB write: the planner is never run here, only the debounce is observed.
		frappe.db.set_value("Marketplace Order", order.name, "erpnext_sales_order", "SO-FAKE-164")

	def test_new_return_on_a_linked_order_bumps_the_debounce(self):
		order = _make_order(self.account, "FBPI-BUMP")
		self._link_sales_order(order)

		returns.upsert_return_item(
			self.account, _return_item("PI-BUMP", order_nr="FBPI-BUMP"), "Manual Lookup"
		)

		debounce = self._debounce(order)
		self.assertIsNotNone(debounce.sync_debounce_until)
		self.assertEqual(debounce.pending_sync_trigger_source, "Manual")

	def test_awb_scan_return_uses_the_scheduled_trigger_source(self):
		order = _make_order(self.account, "FBPI-BUMP-AWB")
		self._link_sales_order(order)

		returns.upsert_return_item(
			self.account, _return_item("PI-BUMP-AWB", order_nr="FBPI-BUMP-AWB"), "AWB Scan"
		)

		self.assertEqual(self._debounce(order).pending_sync_trigger_source, "Scheduled Reconciliation")

	def test_order_without_a_sales_order_is_not_bumped(self):
		order = _make_order(self.account, "FBPI-NO-SO")

		returns.upsert_return_item(
			self.account, _return_item("PI-NO-SO", order_nr="FBPI-NO-SO"), "Manual Lookup"
		)

		self.assertIsNone(self._debounce(order).sync_debounce_until)

	def test_unlinked_return_bumps_nothing(self):
		order = _make_order(self.account, "FBPI-OTHER")
		self._link_sales_order(order)

		returns.upsert_return_item(
			self.account, _return_item("PI-NOMATCH", order_nr="NO-SUCH"), "Manual Lookup"
		)

		self.assertIsNone(self._debounce(order).sync_debounce_until)

	def test_repeat_sighting_of_an_unchanged_link_does_not_bump_again(self):
		order = _make_order(self.account, "FBPI-REPEAT")
		self._link_sales_order(order)
		item = _return_item("PI-REPEAT", order_nr="FBPI-REPEAT")
		returns.upsert_return_item(self.account, item, "Manual Lookup")
		frappe.db.set_value(
			"Marketplace Order",
			order.name,
			{"sync_debounce_until": None, "pending_sync_trigger_source": None},
		)

		returns.upsert_return_item(self.account, item, "AWB Scan")

		self.assertIsNone(self._debounce(order).sync_debounce_until)


class IntegrationTestAwbReferenceMatching(IntegrationTestNoonReturnsBase):
	def test_item_without_matching_awb_reference_is_ignored(self):
		item = _return_item(
			purchase_item_nr="PI-AWB-NOMATCH",
			references=[{"barcode_type": "awb_nr", "barcode": "AWB-OTHER"}],
		)
		self.assertFalse(returns._item_has_matching_awb_reference(item, "AWB-TARGET"))

	def test_item_with_matching_awb_reference_is_matched(self):
		item = _return_item(
			purchase_item_nr="PI-AWB-MATCH",
			references=[{"barcode_type": "awb_nr", "barcode": "AWB-TARGET"}],
		)
		self.assertTrue(returns._item_has_matching_awb_reference(item, "AWB-TARGET"))

	@patch("marketplace_integrations.integrations.noon.returns.list_return_references")
	def test_run_awb_scan_ignores_non_matching_item(self, mock_list):
		"""An AWB-scan result without a matching awb_nr reference is ignored -
		no Noon Return row is created for it."""
		mock_list.return_value = [
			_return_item(
				purchase_item_nr="PI-SCAN-IGNORED",
				references=[{"barcode_type": "awb_nr", "barcode": "SOME-OTHER-AWB"}],
			)
		]
		with patch(
			"marketplace_integrations.integrations.noon.returns._account_awb_numbers",
			return_value=["AWB-SCANNED"],
		):
			returns.run_awb_scan(self.account.name)
		self.assertFalse(frappe.db.exists("Noon Return", {"purchase_item_nr": "PI-SCAN-IGNORED"}))

	@patch("marketplace_integrations.integrations.noon.returns.list_return_references")
	def test_run_awb_scan_records_matching_item(self, mock_list):
		mock_list.return_value = [
			_return_item(
				purchase_item_nr="PI-SCAN-MATCHED",
				references=[{"barcode_type": "awb_nr", "barcode": "AWB-SCANNED"}],
			)
		]
		with patch(
			"marketplace_integrations.integrations.noon.returns._account_awb_numbers",
			return_value=["AWB-SCANNED"],
		):
			returns.run_awb_scan(self.account.name)
		doc_name = frappe.db.exists(
			"Noon Return", {"marketplace_account": self.account.name, "purchase_item_nr": "PI-SCAN-MATCHED"}
		)
		self.assertTrue(doc_name)
		self.assertEqual(frappe.db.get_value("Noon Return", doc_name, "found_via"), "AWB Scan")


class IntegrationTestListReturnReferences(IntegrationTestNoonReturnsBase):
	def test_empty_items_writes_success_log_and_no_record(self):
		mock_client = MagicMock()
		mock_client.request.return_value = self._mock_response(200, {"items": []})
		with patch("marketplace_integrations.integrations.noon.returns.NoonClient", return_value=mock_client):
			items = returns.list_return_references(self.account, "BARCODE-EMPTY")

		self.assertEqual(items, [])
		log = frappe.get_last_doc(
			"Marketplace Sync Log",
			filters={"marketplace_account": self.account.name, "sync_type": "Return Lookup"},
		)
		self.assertEqual(log.status, "Success")
		self.assertEqual(
			frappe.db.count(
				"Noon Return", filters={"marketplace_account": self.account.name}
			),
			0,
		)

	def test_non_2xx_response_writes_error_log_and_no_record(self):
		mock_client = MagicMock()
		mock_client.request.return_value = self._mock_response(500, {"message": "boom"})
		mock_client.error_message.return_value = "boom"
		with patch("marketplace_integrations.integrations.noon.returns.NoonClient", return_value=mock_client):
			with self.assertRaises(frappe.ValidationError):
				returns.list_return_references(self.account, "BARCODE-ERROR")

		log = frappe.get_last_doc(
			"Marketplace Sync Log",
			filters={"marketplace_account": self.account.name, "sync_type": "Return Lookup"},
		)
		self.assertEqual(log.status, "Error")
		self.assertEqual(
			frappe.db.count("Noon Return", filters={"marketplace_account": self.account.name}), 0
		)

	def test_transport_error_writes_error_log_and_no_record(self):
		mock_client = MagicMock()
		mock_client.request.side_effect = Exception("connection reset")
		with patch("marketplace_integrations.integrations.noon.returns.NoonClient", return_value=mock_client):
			with self.assertRaises(Exception):
				returns.list_return_references(self.account, "BARCODE-TRANSPORT")

		log = frappe.get_last_doc(
			"Marketplace Sync Log",
			filters={"marketplace_account": self.account.name, "sync_type": "Return Lookup"},
		)
		self.assertEqual(log.status, "Error")
		self.assertEqual(
			frappe.db.count("Noon Return", filters={"marketplace_account": self.account.name}), 0
		)

	def test_no_erpnext_side_effects(self):
		"""Running a full manual-lookup worker never creates a Sales Invoice,
		Payment Entry, GL Entry, Stock Ledger Entry, or Sales Order - pure
		evidence capture (#153 scope). ERPNext has no standalone "Credit
		Note" doctype (it's a Sales Invoice with is_return=1), so that's
		covered by the Sales Invoice count."""
		side_effect_doctypes = [
			"Sales Invoice",
			"Payment Entry",
			"GL Entry",
			"Stock Ledger Entry",
			"Sales Order",
		]
		counts_before = {doctype: frappe.db.count(doctype) for doctype in side_effect_doctypes}

		mock_client = MagicMock()
		mock_client.request.return_value = self._mock_response(
			200, {"items": [_return_item(purchase_item_nr="PI-NOEFFECT")]}
		)
		with patch("marketplace_integrations.integrations.noon.returns.NoonClient", return_value=mock_client):
			returns.run_manual_lookup(self.account.name, "BARCODE-NOEFFECT")

		for doctype in side_effect_doctypes:
			self.assertEqual(
				frappe.db.count(doctype),
				counts_before[doctype],
				f"{doctype} row count changed - #153 must never create one",
			)
		self.assertTrue(frappe.db.exists("Noon Return", {"purchase_item_nr": "PI-NOEFFECT"}))


class IntegrationTestMerchantCodes(IntegrationTestNoonReturnsBase):
	def test_no_imported_orders_omits_merchant_codes(self):
		self.assertIsNone(returns._account_merchant_codes(self.account))

	def test_imported_order_payload_derives_merchant_codes(self):
		upsert_marketplace_order(
			self.account.name,
			{
				"external_order_id": "ORD-MC-1",
				"latest_payload": {"merchant_code": "MC-ABC", "items": []},
				"items": [],
			},
		)
		codes = returns._account_merchant_codes(self.account)
		self.assertEqual(codes, ["MC-ABC"])

	def test_merchant_codes_capped_at_200_most_recent(self):
		"""Review finding #4: more than 200 distinct merchant_codes are
		truncated to the 200 most-recently-modified orders' codes, not an
		arbitrary/unstable subset."""
		for i in range(205):
			upsert_marketplace_order(
				self.account.name,
				{
					"external_order_id": f"ORD-MC-CAP-{i:03d}",
					"latest_payload": {"merchant_code": f"MC-CAP-{i:03d}", "items": []},
					"items": [],
				},
			)
		codes = returns._account_merchant_codes(self.account)
		self.assertEqual(len(codes), 200)
		# The 5 oldest (first-created/modified) codes must have been dropped,
		# keeping the most recently seen 200 instead.
		for i in range(5):
			self.assertNotIn(f"MC-CAP-{i:03d}", codes)
		self.assertIn("MC-CAP-204", codes)


class IntegrationTestOrderLinkingAmbiguity(IntegrationTestNoonReturnsBase):
	def test_ambiguous_order_nr_leaves_return_unlinked(self):
		"""#153 review finding #5: more than one Marketplace Order matching
		order_nr (across external_order_id and platform_order_reference
		combined) must leave the return unlinked, not pick an arbitrary first
		match."""
		_make_order(self.account, "DUPLICATE-NR", mp_order_nr="OTHER-1")
		_make_order(self.account, "OTHER-2", mp_order_nr="DUPLICATE-NR")

		item = _return_item(purchase_item_nr="PI-AMBIGUOUS", order_nr="DUPLICATE-NR")
		doc = returns.upsert_return_item(self.account, item, "Manual Lookup")
		self.assertFalse(doc.marketplace_order)

	def test_zero_matches_leaves_return_unlinked(self):
		self.assertIsNone(returns._resolve_order_link(self.account, "NO-SUCH-ORDER-NR"))

	def test_exactly_one_match_links(self):
		order = _make_order(self.account, "FBPI-SOLE")
		self.assertEqual(returns._resolve_order_link(self.account, "FBPI-SOLE"), order.name)


class IntegrationTestUpsertReturnItemRace(IntegrationTestNoonReturnsBase):
	def test_concurrent_duplicate_insert_retries_as_update(self):
		"""#153 review finding #2: a stale existing_name read (None) followed
		by a real unique-constraint collision at insert time must roll back
		and retry once as an update against the row the concurrent winner
		already committed - not crash."""
		item = _return_item(purchase_item_nr="PI-RACE")
		winner = frappe.get_doc(
			{
				"doctype": "Noon Return",
				"marketplace_account": self.account.name,
				"purchase_item_nr": "PI-RACE",
				"found_via": "Manual Lookup",
				"first_seen_on": now_datetime(),
				"last_seen_on": now_datetime(),
			}
		).insert(ignore_permissions=True)
		frappe.db.commit()

		real_get_value = frappe.db.get_value
		call_count = {"n": 0}

		def fake_get_value(doctype, filters=None, fieldname="name", *args, **kwargs):
			if doctype == "Noon Return" and isinstance(filters, dict) and "return_identity_key" in filters:
				call_count["n"] += 1
				if call_count["n"] == 1:
					# Simulate a stale read: as far as this caller can tell,
					# nothing exists yet for this identity key.
					return None
			return real_get_value(doctype, filters, fieldname, *args, **kwargs)

		with patch("frappe.db.get_value", side_effect=fake_get_value):
			result = returns.upsert_return_item(self.account, item, "Manual Lookup")

		self.assertEqual(result.name, winner.name)
		self.assertEqual(
			frappe.db.count(
				"Noon Return", {"marketplace_account": self.account.name, "purchase_item_nr": "PI-RACE"}
			),
			1,
		)


class IntegrationTestAccountAwbNumbers(IntegrationTestNoonReturnsBase):
	def _make_order_with_shipment(self, fbpi_order_nr, shipment_nr, status, awb_nr, modified=None):
		order = _make_order(self.account, fbpi_order_nr)
		order.append(
			"noon_shipments", {"integration_shipment_nr": shipment_nr, "status": status, "awb_source": "Own Courier"}
		)
		order.append(
			"noon_shipment_awbs",
			{
				"integration_shipment_nr": shipment_nr,
				"courier": "test-courier",
				"awb_nr": awb_nr,
				"source": "Own Courier",
				"status": "Added",
			},
		)
		order.save(ignore_permissions=True)
		if modified is not None:
			frappe.db.set_value(
				"Marketplace Order Shipment",
				{"parent": order.name, "integration_shipment_nr": shipment_nr},
				"modified",
				modified,
				update_modified=False,
			)
		return order

	def test_cancelled_shipment_awb_excluded(self):
		"""#153 review finding #3: a Cancelled shipment's AWB is excluded from
		the scan."""
		self._make_order_with_shipment("FBPI-CANCEL", "FBPI-CANCEL-1", "Cancelled", "AWB-CANCELLED")
		self._make_order_with_shipment("FBPI-ACTIVE", "FBPI-ACTIVE-1", "Created", "AWB-ACTIVE")

		awbs = returns._account_awb_numbers(self.account)
		self.assertIn("AWB-ACTIVE", awbs)
		self.assertNotIn("AWB-CANCELLED", awbs)

	def test_discarded_shipment_awb_excluded(self):
		"""A Discarded shipment's AWB is likewise excluded."""
		self._make_order_with_shipment("FBPI-DISCARD", "FBPI-DISCARD-1", "Discarded", "AWB-DISCARDED")

		awbs = returns._account_awb_numbers(self.account)
		self.assertNotIn("AWB-DISCARDED", awbs)

	def test_stale_shipment_outside_lookback_excluded(self):
		"""A shipment last modified before the lookback window is excluded
		even though it is still in a non-terminal status."""
		stale_modified = frappe.utils.add_to_date(now_datetime(), days=-(AWB_SCAN_LOOKBACK_DAYS + 1))
		self._make_order_with_shipment(
			"FBPI-STALE", "FBPI-STALE-1", "Created", "AWB-STALE", modified=stale_modified
		)
		self._make_order_with_shipment("FBPI-FRESH", "FBPI-FRESH-1", "Created", "AWB-FRESH")

		awbs = returns._account_awb_numbers(self.account)
		self.assertIn("AWB-FRESH", awbs)
		self.assertNotIn("AWB-STALE", awbs)


class IntegrationTestRunAwbScanIsolation(IntegrationTestNoonReturnsBase):
	@patch("marketplace_integrations.integrations.noon.returns.upsert_return_item")
	@patch("marketplace_integrations.integrations.noon.returns.list_return_references")
	def test_one_awb_upsert_failure_does_not_abort_remaining_awbs(self, mock_list, mock_upsert):
		"""#153 review finding #1: a failure from upsert_return_item() for one
		AWB (not just a list_return_references() failure) must not stop the
		scan from processing the remaining AWBs."""

		def list_side_effect(account, awb_nr, merchant_codes=None, trigger_source=None):
			return [
				_return_item(
					purchase_item_nr=f"PI-{awb_nr}",
					references=[{"barcode_type": "awb_nr", "barcode": awb_nr}],
				)
			]

		mock_list.side_effect = list_side_effect
		mock_upsert.side_effect = [Exception("boom"), MagicMock(name="ok")]

		with patch(
			"marketplace_integrations.integrations.noon.returns._account_awb_numbers",
			return_value=["AWB-FIRST", "AWB-SECOND"],
		):
			returns.run_awb_scan(self.account.name)

		self.assertEqual(mock_upsert.call_count, 2)

	@patch("marketplace_integrations.integrations.noon.returns._account_merchant_codes")
	@patch("marketplace_integrations.integrations.noon.returns.list_return_references")
	def test_merchant_codes_computed_once_per_scan_run(self, mock_list, mock_merchant_codes):
		"""#153 review finding #3: merchant_codes must be derived ONCE per
		scan run, not once per AWB (the N x M recomputation bug)."""
		mock_merchant_codes.return_value = ["MC-ONCE"]
		mock_list.return_value = []

		with patch(
			"marketplace_integrations.integrations.noon.returns._account_awb_numbers",
			return_value=["AWB-1", "AWB-2", "AWB-3"],
		):
			returns.run_awb_scan(self.account.name)

		mock_merchant_codes.assert_called_once()
		self.assertEqual(mock_merchant_codes.call_args[0][0].name, self.account.name)
		self.assertEqual(mock_list.call_count, 3)
		for call in mock_list.call_args_list:
			self.assertEqual(call.kwargs.get("merchant_codes"), ["MC-ONCE"])
			self.assertEqual(call.kwargs.get("trigger_source"), "Scheduled Reconciliation")
