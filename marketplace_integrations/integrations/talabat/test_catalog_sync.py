# Copyright (c) 2026, Tamwenat El Zekrayat and contributors
# For license information, please see license.txt

from unittest.mock import MagicMock, patch

import frappe
from frappe.tests import IntegrationTestCase
from frappe.utils import add_days, nowdate

from marketplace_integrations.integrations.talabat import catalog_sync

# Records registered in setUp/tests; drained by _cleanup_created() in tearDown.
_CREATED = []


def _track(doc):
	_CREATED.append(doc)
	return doc


def _cleanup_created():
	"""Delete tracked Marketplace Accounts (with their Mappings) and then their Talabat Connections.
	Names are re-checked against the DB so fixtures that never got inserted are skipped."""
	accounts = [d.name for d in _CREATED if d.doctype == "Marketplace Account"]
	connections = {d.name for d in _CREATED if d.doctype == "Talabat Connection"}
	_CREATED.clear()
	for name in accounts:
		if not frappe.db.exists("Marketplace Account", name):
			continue
		connections.add(frappe.db.get_value("Marketplace Account", name, "connection"))
		frappe.db.delete("Marketplace Product Mapping", {"marketplace_account": name})
		frappe.delete_doc("Marketplace Account", name, ignore_permissions=True, force=True)
	for name in connections:
		if name and frappe.db.exists("Talabat Connection", name):
			frappe.delete_doc("Talabat Connection", name, ignore_permissions=True, force=True)


def _response(body, status_code=202):
	response = MagicMock()
	response.status_code = status_code
	response.json.return_value = body
	return response


class IntegrationTestTalabatCatalogSync(IntegrationTestCase):
	def setUp(self):
		self.suffix = frappe.generate_hash(length=8)
		self.warehouse = frappe.get_all(
			"Warehouse", filters={"disabled": 0, "is_group": 0}, pluck="name", limit=1
		)[0]
		self.price_list = frappe.get_all(
			"Price List", filters={"selling": 1, "enabled": 1}, pluck="name", limit=1
		)[0]
		self.connection = frappe.get_doc(
			{
				"doctype": "Talabat Connection",
				"connection_name": f"Catalog Sync Connection {self.suffix}",
				"client_id": "test-client-id",
				"client_secret": "test-client-secret",
				"chain_id": f"test-chain-{self.suffix}",
				"enabled": 1,
			}
		).insert()
		_track(self.connection)
		self.account = frappe.get_doc(
			{
				"doctype": "Marketplace Account",
				"account_name": f"Catalog Sync Account {self.suffix}",
				"platform": "Talabat",
				"external_account_id": f"test-vendor-{self.suffix}",
				"connection_doctype": "Talabat Connection",
				"connection": self.connection.name,
				"warehouse": self.warehouse,
				"erpnext_selling_price_list": self.price_list,
				"enabled": 1,
			}
		).insert()
		_track(self.account)

	def tearDown(self):
		_cleanup_created()

	def _make_item(self, marker, disabled=0, barcode=None, sales_uom=None, conversion_factor=None):
		doc = frappe.get_doc(
			{
				"doctype": "Item",
				"item_code": f"TEST-CATALOG-SYNC-{marker}-{self.suffix}",
				"item_group": "Products",
				"stock_uom": "Nos",
				"is_stock_item": 1,
				"disabled": disabled,
			}
		)
		if sales_uom:
			doc.append("uoms", {"uom": sales_uom, "conversion_factor": conversion_factor})
		if barcode:
			doc.append("barcodes", {"barcode": barcode})
		return doc.insert()

	def _make_mapping(self, item, sku, **kwargs):
		doc = {
			"doctype": "Marketplace Product Mapping",
			"marketplace_account": self.account.name,
			"item": item.name,
			"external_reference": sku,
			"sales_uom": item.stock_uom,
			"enabled": 1,
		}
		doc.update(kwargs)
		return frappe.get_doc(doc).insert()

	def _make_price(self, item, rate, uom="Nos", **kwargs):
		doc = {
			"doctype": "Item Price",
			"price_list": self.price_list,
			"item_code": item.name,
			"uom": uom,
			"price_list_rate": rate,
			"selling": 1,
		}
		doc.update(kwargs)
		return frappe.get_doc(doc).insert()

	def _set_actual_qty(self, item, qty):
		bin_name = frappe.db.get_value("Bin", {"item_code": item.name, "warehouse": self.warehouse}, "name")
		if bin_name:
			frappe.db.set_value("Bin", bin_name, "actual_qty", qty)
		else:
			frappe.get_doc(
				{"doctype": "Bin", "item_code": item.name, "warehouse": self.warehouse, "actual_qty": qty}
			).insert()

	def test_builds_active_product_from_warehouse_qty_price_and_first_barcode(self):
		item = self._make_item("ACTIVE", barcode="first-barcode")
		mapping = self._make_mapping(item, "reference-sku", external_sku=" explicit-sku ")
		self._make_price(item, 12.5)
		self._set_actual_qty(item, 7)

		product = catalog_sync.build_catalog_product(self.account, mapping)

		self.assertEqual(
			product,
			{"sku": "explicit-sku", "active": True, "quantity": 7, "price": 12.5, "barcode": "first-barcode"},
		)

	def test_disabled_mapping_or_item_sends_inactive_zero_quantity(self):
		item = self._make_item("DISABLED", disabled=1)
		mapping = self._make_mapping(item, "disabled-sku", enabled=0)
		self._make_price(item, 9)
		self._set_actual_qty(item, 11)

		product = catalog_sync.build_catalog_product(self.account, mapping)

		self.assertFalse(product["active"])
		self.assertEqual(product["quantity"], 0)
		self.assertEqual(product["price"], 9.0)

	def test_listing_uom_converts_quantity_and_requires_an_item_conversion_row(self):
		uom = f"Catalog Box {self.suffix}"
		frappe.get_doc({"doctype": "UOM", "uom_name": uom}).insert()
		item = self._make_item("UOM", sales_uom=uom, conversion_factor=4)
		mapping = self._make_mapping(item, "uom-sku", sales_uom=uom)
		self._make_price(item, 20, uom=uom)
		self._set_actual_qty(item, 11)

		self.assertEqual(catalog_sync.build_catalog_product(self.account, mapping)["quantity"], 2)

		item.uoms = []
		item.save()
		with self.assertRaises(catalog_sync.CatalogBuildError):
			catalog_sync.build_catalog_product(self.account, mapping)

	def _make_bundle_parent(self, marker, components):
		parent = frappe.get_doc(
			{
				"doctype": "Item",
				"item_code": f"TEST-CATALOG-SYNC-{marker}-{self.suffix}",
				"item_group": "Products",
				"stock_uom": "Nos",
				"is_stock_item": 0,
			}
		).insert()
		# ERPNext rejects a bundle with no rows at insert, so an empty (invalid) bundle is built
		# legitimately with one component and then emptied with a direct DB write.
		rows = [(item, qty) for item, qty in components] or [(self._make_item(f"{marker}PH"), 1)]
		bundle = frappe.get_doc(
			{
				"doctype": "Product Bundle",
				"new_item_code": parent.name,
				"items": [{"item_code": item.name, "qty": qty} for item, qty in rows],
			}
		).insert(ignore_permissions=True)
		if not components:
			frappe.db.delete("Product Bundle Item", {"parent": bundle.name})
		return parent

	def test_bundle_parent_quantity_comes_from_its_components(self):
		part = self._make_item("BUNDLEPART")
		self._set_actual_qty(part, 11)
		parent = self._make_bundle_parent("BUNDLE", [(part, 2)])
		mapping = self._make_mapping(parent, "bundle-sku")
		self._make_price(parent, 10)

		self.assertEqual(catalog_sync.build_catalog_product(self.account, mapping)["quantity"], 5)

	def test_invalid_bundle_is_a_build_error_not_a_zero_quantity(self):
		parent = self._make_bundle_parent("BADBUNDLE", [])
		mapping = self._make_mapping(parent, "bad-bundle-sku")
		self._make_price(parent, 10)

		built = catalog_sync.build_catalog_batch(self.account, [mapping.name])

		self.assertEqual(built["batches"], [])
		self.assertEqual([row["mapping_name"] for row in built["errors"]], [mapping.name])
		self.assertIn("no component", built["errors"][0]["error"])

	def test_price_prefers_current_exact_uom_general_item_price(self):
		item = self._make_item("PRICE")
		mapping = self._make_mapping(item, "price-sku")
		self._make_price(item, 5, valid_from=add_days(nowdate(), -3), valid_upto=add_days(nowdate(), -1))
		self._make_price(item, 11, valid_from=add_days(nowdate(), -1))
		self._make_price(item, 12, valid_from=nowdate())

		self.assertEqual(catalog_sync.get_listing_price(self.account, mapping, item), 12)

	def test_missing_price_only_marks_that_mapping_error_and_submits_others(self):
		valid_item = self._make_item("VALID")
		invalid_item = self._make_item("INVALID")
		valid = self._make_mapping(valid_item, "valid-sku")
		invalid = self._make_mapping(invalid_item, "invalid-sku")
		self._make_price(valid_item, 10)

		with patch(
			"marketplace_integrations.integrations.talabat.catalog_sync.TalabatClient.request"
		) as request:
			request.return_value = _response({"job_id": "catalog-job-1", "job_status": "QUEUED"})
			result = catalog_sync.sync_catalog(self.account.name)

		valid.reload()
		invalid.reload()
		self.assertTrue(result["batches"][0]["ok"])
		self.assertEqual(valid.sync_status, "Queued")
		self.assertEqual(valid.last_sync_reference, "catalog-job-1")
		self.assertEqual(invalid.sync_status, "Error")
		self.assertIn("no current Item Price", invalid.last_error)
		self.assertEqual(
			request.call_args.kwargs["json"],
			{"products": [{"sku": "valid-sku", "active": True, "quantity": 0, "price": 10.0}]},
		)

	def test_sync_uses_202_job_and_never_marks_mapping_synced(self):
		item = self._make_item("JOB")
		mapping = self._make_mapping(item, "job-sku")
		self._make_price(item, 10)

		with patch(
			"marketplace_integrations.integrations.talabat.catalog_sync.TalabatClient.request"
		) as request:
			request.return_value = _response({"job_id": "catalog-job-2", "job_status": "QUEUED"})
			catalog_sync.sync_catalog(self.account.name)

		mapping.reload()
		self.assertEqual(mapping.sync_status, "Queued")
		self.assertEqual(mapping.last_sync_reference, "catalog-job-2")
		self.assertIsNotNone(mapping.last_synced_on)
		request.assert_called_once_with(
			"PUT",
			f"/v2/chains/{self.connection.chain_id}/vendors/{self.account.external_account_id}/catalog",
			json={"products": [{"sku": "job-sku", "active": True, "quantity": 0, "price": 10.0}]},
		)

	def test_failed_talabat_batch_marks_error_without_fake_job(self):
		item = self._make_item("FAIL")
		mapping = self._make_mapping(item, "fail-sku")
		self._make_price(item, 10)

		with patch(
			"marketplace_integrations.integrations.talabat.catalog_sync.TalabatClient.request"
		) as request:
			request.return_value = _response({"error": "untrusted"}, status_code=500)
			catalog_sync.sync_catalog(self.account.name)

		mapping.reload()
		self.assertEqual(mapping.sync_status, "Error")
		self.assertFalse(mapping.last_sync_reference)
		self.assertEqual(mapping.last_error, catalog_sync.REMOTE_ERROR)

	def test_batching_never_submits_more_than_the_configured_cap(self):
		for index in range(3):
			item = self._make_item(f"BATCH-{index}")
			self._make_mapping(item, f"batch-sku-{index}")
			self._make_price(item, index + 1)

		with (
			patch("marketplace_integrations.integrations.talabat.catalog_sync.CATALOG_BATCH_SIZE", 2),
			patch(
				"marketplace_integrations.integrations.talabat.catalog_sync.TalabatClient.request"
			) as request,
		):
			request.side_effect = [
				_response({"job_id": "catalog-job-3", "job_status": "QUEUED"}),
				_response({"job_id": "catalog-job-4", "job_status": "QUEUED"}),
			]
			catalog_sync.sync_catalog(self.account.name)

		self.assertEqual(request.call_count, 2)
		self.assertEqual(len(request.call_args_list[0].kwargs["json"]["products"]), 2)
		self.assertEqual(len(request.call_args_list[1].kwargs["json"]["products"]), 1)

	def test_disabled_warehouse_blocks_catalog_sync(self):
		"""#85 review finding #7: validate_catalog_sync_account() now goes
		through the shared services/account_warehouse.py helper, which also
		checks Warehouse.disabled - not previously reachable through this
		call site."""
		self.account.reload()
		self.account.warehouse = (
			frappe.get_doc(
				{
					"doctype": "Warehouse",
					"warehouse_name": f"Catalog Sync Disabled WH {self.suffix}",
					"company": frappe.get_cached_value("Warehouse", self.warehouse, "company"),
					"disabled": 1,
				}
			)
			.insert()
			.name
		)
		self.account.save()

		with self.assertRaises(frappe.ValidationError) as context:
			catalog_sync.validate_catalog_sync_account(self.account.name)
		self.assertIn("disabled", str(context.exception).lower())

	def test_warehouse_belonging_to_a_different_company_blocks_catalog_sync_when_account_has_a_company(self):
		"""#85 review finding #7: the shared helper's Company-match check is a
		no-op when Marketplace Account.erpnext_company is unset (the normal
		catalog-sync-only case), but must still fire once a Company is set."""
		warehouse_company = frappe.get_cached_value("Warehouse", self.warehouse, "company")
		other_company = frappe.get_all(
			"Company", filters={"name": ["!=", warehouse_company]}, pluck="name", limit=1
		)
		if not other_company:
			other_company = [
				frappe.get_doc(
					{
						"doctype": "Company",
						"company_name": f"Catalog Sync Other Co {self.suffix}",
						"default_currency": frappe.get_cached_value(
							"Company", warehouse_company, "default_currency"
						),
						"country": frappe.get_cached_value("Company", warehouse_company, "country"),
					}
				)
				.insert()
				.name
			]

		self.account.reload()
		self.account.erpnext_company = other_company[0]
		self.account.save()

		with self.assertRaises(frappe.ValidationError) as context:
			catalog_sync.validate_catalog_sync_account(self.account.name)
		self.assertIn("company", str(context.exception).lower())
