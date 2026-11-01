# Copyright (c) 2026, Tamwenat El Zekrayat and contributors
# For license information, please see license.txt

"""Mocked test of the retiring patch (#415): the old Custom Fields, Property Setters and columns go, a second
run does nothing, and the Noon AWB and shipment child tables are not touched. No database is changed."""

from unittest.mock import patch

from frappe.tests import UnitTestCase

from marketplace_integrations.patches.v0_0 import retire_delivery_note_carrier_status_fields as retire

_DB = "marketplace_integrations.patches.v0_0.retire_delivery_note_carrier_status_fields.frappe"


class UnitTestRetireDeliveryNoteCarrierStatusFields(UnitTestCase):
	def _run(self, custom_fields=(), setters=(), columns=()):
		"""Run the patch against a fake database holding these Custom Field names, field names with a Property
		Setter and column names. Returns the mocked ``frappe``."""
		with patch(_DB) as frappe_mock:
			db = frappe_mock.db
			db.exists.side_effect = lambda doctype, filters: (
				filters in custom_fields
				if doctype == "Custom Field"
				else filters.get("field_name") in setters
			)
			db.has_column.side_effect = lambda doctype, fieldname: fieldname in columns
			retire.execute()
		return frappe_mock

	def test_it_removes_the_custom_fields_property_setters_and_columns(self):
		frappe_mock = self._run(
			custom_fields=("Delivery Note-custom_carrier_status", "Delivery Note-custom_carrier_error"),
			setters=("custom_carrier_status", "custom_carrier_error"),
			columns=("custom_carrier_status", "custom_carrier_error"),
		)

		deleted = [(call.args[0], call.args[1]) for call in frappe_mock.db.delete.call_args_list]
		self.assertIn(("Custom Field", {"name": "Delivery Note-custom_carrier_status"}), deleted)
		self.assertIn(("Custom Field", {"name": "Delivery Note-custom_carrier_error"}), deleted)
		self.assertIn(
			("Property Setter", {"doc_type": "Delivery Note", "field_name": "custom_carrier_status"}), deleted
		)
		self.assertIn(
			("Property Setter", {"doc_type": "Delivery Note", "field_name": "custom_carrier_error"}), deleted
		)
		statements = [call.args[0] for call in frappe_mock.db.sql_ddl.call_args_list]
		self.assertEqual(
			statements,
			[
				"ALTER TABLE `tabDelivery Note` DROP COLUMN `custom_carrier_status`",
				"ALTER TABLE `tabDelivery Note` DROP COLUMN `custom_carrier_error`",
			],
		)
		frappe_mock.clear_cache.assert_called_once_with(doctype="Delivery Note")

	def test_a_second_run_finds_nothing_and_changes_nothing(self):
		frappe_mock = self._run()

		frappe_mock.db.delete.assert_not_called()
		frappe_mock.db.sql_ddl.assert_not_called()
		frappe_mock.clear_cache.assert_not_called()

	def test_a_partly_applied_run_only_does_what_is_left(self):
		frappe_mock = self._run(columns=("custom_carrier_error",))

		frappe_mock.db.delete.assert_not_called()
		self.assertEqual(
			[call.args[0] for call in frappe_mock.db.sql_ddl.call_args_list],
			["ALTER TABLE `tabDelivery Note` DROP COLUMN `custom_carrier_error`"],
		)

	def test_the_noon_awb_and_shipment_tables_are_never_touched(self):
		frappe_mock = self._run(
			custom_fields=("Delivery Note-custom_carrier_status",),
			setters=("custom_carrier_status",),
			columns=("custom_carrier_status",),
		)

		touched = " ".join(
			str(call) for call in frappe_mock.db.delete.call_args_list + frappe_mock.db.sql_ddl.call_args_list
		)
		self.assertNotIn("Noon AWB", touched)
		self.assertNotIn("Marketplace Order Shipment", touched)
