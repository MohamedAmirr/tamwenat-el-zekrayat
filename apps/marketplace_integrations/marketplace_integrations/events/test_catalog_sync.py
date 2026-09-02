# Copyright (c) 2026, Tamwenat El Zekrayat and contributors
# For license information, please see license.txt

from unittest.mock import MagicMock, call, patch

import frappe
from frappe.tests import UnitTestCase

from marketplace_integrations.events import catalog_sync


def _doc(**values):
	doc = frappe._dict(values)
	doc.get_doc_before_save = lambda: values.get("_previous")
	return doc


class UnitTestCatalogSyncEvents(UnitTestCase):
	def setUp(self):
		self.cache = MagicMock()
		self.cache.make_key.side_effect = lambda key: key
		self.cache.set.return_value = True

	def test_stock_event_enqueues_only_post_commit_worker(self):
		with (
			patch("marketplace_integrations.events.catalog_sync.frappe.cache", return_value=self.cache),
			patch("marketplace_integrations.events.catalog_sync.frappe.enqueue") as enqueue,
			patch("marketplace_integrations.events.catalog_sync.catalog_sync.sync_catalog") as sync_catalog,
		):
			catalog_sync.stock_ledger_entry_after_insert(_doc(item_code="ITEM-1", warehouse="WH-1"))

		enqueue.assert_called_once_with(
			catalog_sync.sync_item_warehouse,
			queue="long",
			timeout=3600,
			enqueue_after_commit=True,
			item_code="ITEM-1",
			warehouse="WH-1",
		)
		sync_catalog.assert_not_called()

	def test_no_bin_document_event_is_registered(self):
		self.assertNotIn("Bin", frappe.get_hooks("doc_events"))

	def test_duplicate_stock_signal_is_coalesced(self):
		self.cache.set.side_effect = [True, False]
		with (
			patch("marketplace_integrations.events.catalog_sync.frappe.cache", return_value=self.cache),
			patch("marketplace_integrations.events.catalog_sync.frappe.enqueue") as enqueue,
		):
			catalog_sync.queue_item_warehouse_sync("ITEM-1", "WH-1")
			catalog_sync.queue_item_warehouse_sync("ITEM-1", "WH-1")

		self.assertEqual(enqueue.call_count, 1)
		self.assertEqual(self.cache.set.call_args.kwargs, {"nx": True, "ex": 60})

	def test_price_update_queues_old_and_new_general_scopes(self):
		previous = _doc(
			item_code="ITEM-1",
			price_list="Old Selling",
			uom="Nos",
			customer="",
			supplier="",
			price_list_rate=5,
		)
		doc = _doc(
			item_code="ITEM-1",
			price_list="New Selling",
			uom="Nos",
			customer="",
			supplier="",
			price_list_rate=5,
			_previous=previous,
		)
		with patch("marketplace_integrations.events.catalog_sync.queue_item_price_scope") as queue:
			catalog_sync.item_price_on_update(doc)

		self.assertEqual(
			queue.call_args_list,
			[call("ITEM-1", "Old Selling", "Nos"), call("ITEM-1", "New Selling", "Nos")],
		)

	def test_party_specific_price_never_queues_a_marketplace_scope(self):
		with patch("marketplace_integrations.events.catalog_sync.queue_item_price_scope") as queue:
			catalog_sync.item_price_after_insert(
				_doc(item_code="ITEM-1", price_list="Selling", uom="Nos", customer="Customer", supplier="")
			)
		queue.assert_not_called()

	def test_mapping_sync_metadata_change_does_not_requeue(self):
		previous = _doc(
			marketplace_account="ACCOUNT-1",
			item="ITEM-1",
			sales_uom="Nos",
			external_sku="SKU-1",
			external_reference="SKU-1",
			enabled=1,
			sync_status="Queued",
		)
		doc = _doc(
			name="MPM-1",
			marketplace_account="ACCOUNT-1",
			item="ITEM-1",
			sales_uom="Nos",
			external_sku="SKU-1",
			external_reference="SKU-1",
			enabled=1,
			sync_status="Synced",
			_previous=previous,
		)
		with patch("marketplace_integrations.events.catalog_sync.queue_mapping_sync") as queue:
			catalog_sync.marketplace_product_mapping_on_update(doc)
		queue.assert_not_called()

	def test_item_catalog_changes_queue_all_its_mappings(self):
		previous = _doc(
			name="ITEM-1",
			disabled=0,
			stock_uom="Nos",
			barcodes=[frappe._dict(idx=1, barcode="OLD")],
			uoms=[frappe._dict(uom="Box", conversion_factor=10)],
		)
		doc = _doc(
			name="ITEM-1",
			disabled=1,
			stock_uom="Box",
			barcodes=[frappe._dict(idx=1, barcode="NEW")],
			uoms=[frappe._dict(uom="Box", conversion_factor=12)],
			_previous=previous,
		)
		with (
			patch(
				"marketplace_integrations.events.catalog_sync._mapped_listing_uoms",
				return_value={"Box"},
			),
			patch("marketplace_integrations.events.catalog_sync.queue_item_mappings") as queue,
		):
			catalog_sync.item_on_update(doc)

		queue.assert_called_once_with("ITEM-1")

	def test_mapping_identity_change_queues_previous_state_for_deactivation(self):
		previous = _doc(
			marketplace_account="ACCOUNT-1",
			item="ITEM-1",
			sales_uom="Nos",
			external_sku="SKU-OLD",
			external_reference="REF-OLD",
			enabled=1,
		)
		doc = _doc(
			name="MPM-1",
			marketplace_account="ACCOUNT-1",
			item="ITEM-1",
			sales_uom="Nos",
			external_sku="SKU-NEW",
			external_reference="REF-NEW",
			enabled=1,
			_previous=previous,
		)
		with patch("marketplace_integrations.events.catalog_sync.queue_mapping_sync") as queue:
			catalog_sync.marketplace_product_mapping_on_update(doc)

		queue.assert_called_once_with("MPM-1", catalog_sync._mapping_state(previous))

	def test_mapping_identity_cache_key_is_deterministic(self):
		first = {"item": "ITEM-1", "marketplace_account": "ACCOUNT-1"}
		second = {"marketplace_account": "ACCOUNT-1", "item": "ITEM-1"}

		self.assertEqual(catalog_sync._identity_key(first), catalog_sync._identity_key(second))

	def test_account_disabled_transition_does_not_queue_but_enable_does(self):
		disabled = _doc(name="ACCOUNT-1", enabled=0, warehouse="WH-1", erpnext_selling_price_list="Selling")
		enabled = _doc(name="ACCOUNT-1", enabled=1, warehouse="WH-1", erpnext_selling_price_list="Selling")
		with patch("marketplace_integrations.events.catalog_sync.queue_account_sync") as queue:
			catalog_sync.marketplace_account_on_update(_doc(**disabled, _previous=enabled))
			catalog_sync.marketplace_account_on_update(_doc(**enabled, _previous=disabled))

		queue.assert_called_once_with("ACCOUNT-1")

	def test_enabled_account_warehouse_or_price_list_change_queues_full_sync(self):
		previous = _doc(name="ACCOUNT-1", enabled=1, warehouse="WH-1", erpnext_selling_price_list="Old")
		with patch("marketplace_integrations.events.catalog_sync.queue_account_sync") as queue:
			catalog_sync.marketplace_account_on_update(
				_doc(
					name="ACCOUNT-1",
					enabled=1,
					warehouse="WH-2",
					erpnext_selling_price_list="Old",
					_previous=previous,
				)
			)
			catalog_sync.marketplace_account_on_update(
				_doc(
					name="ACCOUNT-1",
					enabled=1,
					warehouse="WH-2",
					erpnext_selling_price_list="New",
					_previous=_doc(
						name="ACCOUNT-1", enabled=1, warehouse="WH-2", erpnext_selling_price_list="Old"
					),
				)
			)

		self.assertEqual(queue.call_args_list, [call("ACCOUNT-1"), call("ACCOUNT-1")])

	def test_stock_worker_fans_out_to_all_accounts_sharing_warehouse(self):
		with (
			patch("marketplace_integrations.events.catalog_sync.frappe.get_all") as get_all,
			patch("marketplace_integrations.events.catalog_sync.catalog_sync.sync_catalog") as sync_catalog,
		):
			get_all.side_effect = [["ACCOUNT-1", "ACCOUNT-2"], ["MPM-1"], ["MPM-2"]]
			catalog_sync.sync_item_warehouse("ITEM-1", "WH-1")

		self.assertEqual(
			sync_catalog.call_args_list,
			[call("ACCOUNT-1", ["MPM-1"]), call("ACCOUNT-2", ["MPM-2"])],
		)

	def test_previous_identity_worker_submits_an_inactive_product_before_current_sync(self):
		mapping = _doc(name="MPM-1", marketplace_account="ACCOUNT-NEW")
		previous = {
			"marketplace_account": "ACCOUNT-OLD",
			"item": "ITEM-1",
			"external_sku": "SKU-OLD",
			"external_reference": "REF-OLD",
			"sales_uom": "Nos",
		}
		with (
			patch("marketplace_integrations.events.catalog_sync.frappe.db.exists", return_value=True),
			patch("marketplace_integrations.events.catalog_sync.frappe.get_doc", return_value=mapping),
			patch(
				"marketplace_integrations.events.catalog_sync.catalog_sync.build_catalog_product",
				return_value={"sku": "SKU-OLD"},
			) as build,
			patch(
				"marketplace_integrations.events.catalog_sync.catalog_sync.submit_catalog_products"
			) as submit,
			patch("marketplace_integrations.events.catalog_sync.catalog_sync.sync_catalog") as sync_catalog,
		):
			catalog_sync.sync_mapping("MPM-1", previous)

		self.assertFalse(build.call_args.args[1].enabled)
		submit.assert_called_once_with(
			"ACCOUNT-OLD", [{"mapping_name": "MPM-1", "product": {"sku": "SKU-OLD"}}]
		)
		sync_catalog.assert_called_once_with("ACCOUNT-NEW", ["MPM-1"])
