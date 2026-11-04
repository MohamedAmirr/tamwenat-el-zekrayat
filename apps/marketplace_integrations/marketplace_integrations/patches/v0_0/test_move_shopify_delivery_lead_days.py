# Copyright (c) 2026, Tamwenat El Zekrayat and contributors
# For license information, please see license.txt

"""Mocked test of the patch moving ``shopify_delivery_lead_days`` into ``delivery_lead_days``. No database is changed."""

from unittest.mock import patch

import frappe
from frappe.tests import UnitTestCase

from marketplace_integrations.patches.v0_0 import move_shopify_delivery_lead_days as move

_FRAPPE = "marketplace_integrations.patches.v0_0.move_shopify_delivery_lead_days.frappe"


class UnitTestMoveShopifyDeliveryLeadDays(UnitTestCase):
	def _run(self, columns, old_values=None, new_values=None):
		"""Run against a fake database: column names present, ``{name: old value}``, ``{name: new value}``."""
		old_values = old_values or {}
		new_values = new_values or {}
		with patch(_FRAPPE) as frappe_mock:
			db = frappe_mock.db
			db.has_column.side_effect = lambda doctype, fieldname: fieldname in columns
			db.sql.return_value = [
				frappe._dict(name=name, shopify_delivery_lead_days=value)
				for name, value in old_values.items()
			]
			db.get_value.side_effect = lambda doctype, name, fieldname: new_values.get(name, 0)
			move.execute()
		return frappe_mock

	def test_it_copies_the_value_then_drops_the_old_column(self):
		frappe_mock = self._run(
			columns=("shopify_delivery_lead_days", "delivery_lead_days"), old_values={"Shop": 3}
		)

		frappe_mock.db.set_value.assert_called_once_with(
			"Marketplace Account", "Shop", "delivery_lead_days", 3, update_modified=False
		)
		frappe_mock.db.sql_ddl.assert_called_once_with(
			"ALTER TABLE `tabMarketplace Account` DROP COLUMN `shopify_delivery_lead_days`"
		)

	def test_it_never_overwrites_a_value_already_set(self):
		frappe_mock = self._run(
			columns=("shopify_delivery_lead_days", "delivery_lead_days"),
			old_values={"Shop": 3},
			new_values={"Shop": 5},
		)

		frappe_mock.db.set_value.assert_not_called()

	def test_a_second_run_with_the_old_column_gone_changes_nothing(self):
		frappe_mock = self._run(columns=("delivery_lead_days",))

		frappe_mock.db.sql.assert_not_called()
		frappe_mock.db.set_value.assert_not_called()
		frappe_mock.db.sql_ddl.assert_not_called()

	def test_it_does_nothing_when_the_new_column_is_missing(self):
		frappe_mock = self._run(columns=("shopify_delivery_lead_days",), old_values={"Shop": 3})

		frappe_mock.db.sql.assert_not_called()
		frappe_mock.db.set_value.assert_not_called()
		frappe_mock.db.sql_ddl.assert_not_called()
		frappe_mock.db.delete.assert_not_called()

	def test_it_deletes_orphaned_custom_field_and_property_setter_rows(self):
		with patch(_FRAPPE) as frappe_mock:
			db = frappe_mock.db
			db.has_column.side_effect = lambda doctype, fieldname: fieldname == "delivery_lead_days"
			db.exists.return_value = True
			move.execute()

		db.delete.assert_any_call("Custom Field", {"name": "Marketplace Account-shopify_delivery_lead_days"})
		db.delete.assert_any_call(
			"Property Setter",
			{"doc_type": "Marketplace Account", "field_name": "shopify_delivery_lead_days"},
		)
