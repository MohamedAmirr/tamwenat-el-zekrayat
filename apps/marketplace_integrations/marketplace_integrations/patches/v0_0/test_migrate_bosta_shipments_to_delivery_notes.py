# Copyright (c) 2026, Tamwenat El Zekrayat and contributors
# For license information, please see license.txt

"""The Bosta migration patches (epic #413, #408). The old ``tabBosta Shipment`` table is created by hand
for these tests (the DocType is gone) and dropped again; DDL commits, so everything is cleaned up."""

from unittest.mock import patch

import frappe
from frappe.tests import IntegrationTestCase

from marketplace_integrations.patches.v0_0 import (
	create_bosta_transporter_supplier,
)
from marketplace_integrations.patches.v0_0 import (
	migrate_bosta_shipments_to_delivery_notes as migration,
)
from marketplace_integrations.tests.bosta_fixtures import BostaFixtureMixin

OLD_TABLE = """create table if not exists `tabBosta Shipment` (
	name varchar(140) primary key,
	sales_order varchar(140), delivery_note varchar(140), status varchar(140),
	tracking_number varchar(140), bosta_delivery_id varchar(140), unique_business_reference varchar(140),
	cod_amount decimal(21,9), shipment_fee decimal(21,9), bosta_state_code varchar(20),
	last_bosta_event_timestamp varchar(40), city_code varchar(140), city varchar(140),
	last_bosta_status_on datetime
)"""


class IntegrationTestMigrateBostaShipments(BostaFixtureMixin, IntegrationTestCase):
	def setUp(self):
		super().setUp()
		frappe.db.sql_ddl(OLD_TABLE)
		self.addCleanup(self._drop_old_table)

	def _drop_old_table(self):
		frappe.db.sql_ddl("drop table if exists `tabBosta Shipment`")
		for name in frappe.get_all("Role", filters={"name": "Bosta User"}, pluck="name"):
			frappe.delete_doc("Role", name, force=True, ignore_permissions=True)
		frappe.db.commit()

	def _shipment(self, note, **values):
		row = {
			"name": f"BSH-{note.name}",
			"sales_order": note.items[0].against_sales_order,
			"delivery_note": note.name,
			"status": "Created",
			"tracking_number": "TRK-1",
			"bosta_delivery_id": "DEL-1",
			"unique_business_reference": "REF-1",
			"cod_amount": 120,
			"shipment_fee": 45,
			"bosta_state_code": "45",
			"city_code": "EG-01",
			"city": "Cairo",
			**values,
		}
		columns = ", ".join(f"`{k}`" for k in row)
		marks = ", ".join(["%s"] * len(row))
		frappe.db.sql(f"insert into `tabBosta Shipment` ({columns}) values ({marks})", tuple(row.values()))
		frappe.db.commit()
		return row

	def _note_values(self, note):
		return frappe.db.get_value(
			"Delivery Note",
			note.name,
			[
				"custom_integration_status",
				"custom_carrier_delivery_id",
				"custom_carrier_reference",
				"custom_cod_amount",
				"custom_actual_shipping_cost",
				"custom_shipping_status",
				"custom_bosta_city",
				"lr_no",
				"custom_shipping_label",
			],
			as_dict=True,
		)

	def test_a_shipment_is_copied_onto_its_delivery_note_once(self):
		note = self.make_note(self.make_order())
		self._shipment(note)
		file = frappe.get_doc(
			{
				"doctype": "File",
				"file_name": "label.txt",
				"attached_to_doctype": "Delivery Note",
				"attached_to_name": note.name,
				"content": b"label",
				"is_private": 1,
			}
		)
		file.insert(ignore_permissions=True)
		# As the old flow left it: attached to the shipment (the DocType no longer exists, so set it directly).
		frappe.db.set_value(
			"File", file.name, {"attached_to_doctype": "Bosta Shipment", "attached_to_name": f"BSH-{note.name}"}
		)
		self.addCleanup(lambda: frappe.delete_doc("File", file.name, force=True, ignore_permissions=True))

		migration.execute()
		values = self._note_values(note)
		self.assertEqual(values.custom_integration_status, "Created")
		self.assertEqual(values.custom_carrier_delivery_id, "DEL-1")
		self.assertEqual(values.custom_carrier_reference, "REF-1")
		self.assertEqual(values.custom_cod_amount, 120)
		self.assertEqual(values.custom_actual_shipping_cost, 45)
		self.assertEqual(values.custom_shipping_status, "Delivered")
		self.assertEqual(values.lr_no, "TRK-1")
		self.assertEqual(values.custom_shipping_label, file.file_url)
		self.assertEqual(
			frappe.db.get_value("File", file.name, ["attached_to_doctype", "attached_to_name"]),
			("Delivery Note", note.name),
		)
		logs = frappe.get_all(
			"Integration Request",
			filters={"reference_docname": note.name, "custom_operation_type": "Create"},
			pluck="name",
		)
		self.assertEqual(len(logs), 1)

		# A second run changes nothing and adds no second log row.
		migration.execute()
		self.assertEqual(self._note_values(note), values)
		self.assertEqual(
			frappe.db.count(
				"Integration Request", {"reference_docname": note.name, "custom_operation_type": "Create"}
			),
			1,
		)

	def test_a_canceled_shipment_becomes_a_cancelled_note(self):
		note = self.make_note(self.make_order())
		self._shipment(note, status="Canceled")
		migration.execute()
		self.assertEqual(self._note_values(note).custom_integration_status, "Cancelled")

	def test_the_patch_stops_with_a_list_when_a_live_shipment_has_no_delivery_note(self):
		note = self.make_note(self.make_order())
		row = self._shipment(note, delivery_note="", name="BSH-LOOSE")
		with self.assertRaises(Exception) as caught:
			migration.execute()
		self.assertIn("BSH-LOOSE", str(caught.exception))
		# Nothing was copied.
		self.assertFalse(self._note_values(note).custom_carrier_delivery_id)
		self.assertTrue(row)

	def test_the_bosta_user_role_is_deleted_with_its_assignments(self):
		frappe.get_doc({"doctype": "Role", "role_name": "Bosta User", "desk_access": 1}).insert(ignore_permissions=True)
		user = self.make_user("Stock User", "Bosta User")
		frappe.db.commit()
		frappe.db.sql_ddl("drop table if exists `tabBosta Shipment`")
		migration.execute()
		self.assertFalse(frappe.db.exists("Role", "Bosta User"))
		self.assertFalse(frappe.db.exists("Has Role", {"role": "Bosta User", "parent": user}))
		# and again, with the role already gone
		migration.execute()

	def test_every_unmovable_shipment_is_listed_including_deleted_notes_and_rows_without_ids(self):
		note = self.make_note(self.make_order())
		other = self.make_note(self.make_order())
		self._shipment(note, name="BSH-DELETED", delivery_note="DN-DOES-NOT-EXIST")
		self._shipment(other, name="BSH-NO-IDS", delivery_note="", status="Pending Bosta", tracking_number="", bosta_delivery_id="")
		self._shipment(other, name="BSH-FAILED", delivery_note="", status="Bosta Failed", tracking_number="", bosta_delivery_id="")
		self._shipment(other, name="BSH-CANCELED", delivery_note="", status="Canceled")
		with self.assertRaises(Exception) as caught:
			migration.execute()
		message = str(caught.exception)
		for name in ("BSH-DELETED", "BSH-NO-IDS", "BSH-FAILED"):
			self.assertIn(name, message)
		self.assertNotIn("BSH-CANCELED", message)
		self.assertFalse(self._note_values(other).custom_carrier_delivery_id)

	def test_a_live_shipment_wins_over_a_canceled_one_of_the_same_note_in_any_row_order(self):
		note = self.make_note(self.make_order())
		self._shipment(note, name="BSH-B", status="Created", bosta_delivery_id="DEL-LIVE", tracking_number="TRK-LIVE")
		self._shipment(note, name="BSH-C", status="Canceled", bosta_delivery_id="DEL-OLD", tracking_number="TRK-OLD")
		migration.execute()
		values = self._note_values(note)
		self.assertEqual(values.custom_carrier_delivery_id, "DEL-LIVE")
		self.assertEqual(values.custom_integration_status, "Created")
		self.assertEqual(values.lr_no, "TRK-LIVE")
		before = values
		migration.execute()
		self.assertEqual(self._note_values(note), before)

	def test_two_live_shipments_with_different_delivery_ids_stop_the_patch(self):
		note = self.make_note(self.make_order())
		self._shipment(note, name="BSH-1", bosta_delivery_id="DEL-A")
		self._shipment(note, name="BSH-2", bosta_delivery_id="DEL-B")
		with self.assertRaises(Exception) as caught:
			migration.execute()
		self.assertIn(note.name, str(caught.exception))
		self.assertIn("DEL-A", str(caught.exception))
		self.assertFalse(self._note_values(note).custom_carrier_delivery_id)

	def test_a_note_that_already_holds_another_delivery_stops_the_patch(self):
		note = self.make_note(self.make_order())
		frappe.db.set_value("Delivery Note", note.name, "custom_carrier_delivery_id", "DEL-HELD")
		self._shipment(note, bosta_delivery_id="DEL-OTHER")
		with self.assertRaises(Exception) as caught:
			migration.execute()
		self.assertIn("DEL-HELD", str(caught.exception))
		self.assertEqual(self._note_values(note).custom_carrier_delivery_id, "DEL-HELD")

	def test_the_migrated_note_is_cod_confirmed_and_the_sales_order_summary_is_recalculated(self):
		order = self.make_order()
		note = self.make_note(order)
		self._shipment(note)
		migration.execute()
		self.assertEqual(frappe.db.get_value("Delivery Note", note.name, "custom_cod_confirmed"), 1)
		summary = frappe.db.get_value("Sales Order", order.name, "custom_shipping_status_summary")
		self.assertEqual(frappe.parse_json(summary), {"Delivered": 1})

	def test_the_old_sales_order_delivery_fields_are_deleted_with_their_columns(self):
		names = ("custom_bosta_delivery_tab", "custom_bosta_delivery_html", "custom_bosta_delivery_status")
		for fieldname, fieldtype in zip(names, ("Tab Break", "HTML", "Data"), strict=True):
			if not frappe.db.exists("Custom Field", f"Sales Order-{fieldname}"):
				frappe.get_doc(
					{"doctype": "Custom Field", "dt": "Sales Order", "fieldname": fieldname, "fieldtype": fieldtype, "label": fieldname}
				).insert(ignore_permissions=True)
		frappe.get_doc(
			{
				"doctype": "Property Setter",
				"doc_type": "Sales Order",
				"doctype_or_field": "DocField",
				"field_name": "custom_bosta_delivery_status",
				"property": "hidden",
				"property_type": "Check",
				"value": "1",
			}
		).insert(ignore_permissions=True)
		frappe.db.commit()
		migration.execute()
		for fieldname in names:
			self.assertFalse(frappe.db.exists("Custom Field", f"Sales Order-{fieldname}"))
		self.assertFalse(frappe.db.exists("Property Setter", {"doc_type": "Sales Order", "field_name": "custom_bosta_delivery_status"}))
		self.assertFalse(frappe.db.has_column("Sales Order", "custom_bosta_delivery_status"))
		migration.execute()

	def test_the_patch_fails_loudly_when_the_doctype_row_exists_but_the_table_does_not(self):
		frappe.db.sql_ddl("drop table if exists `tabBosta Shipment`")
		real_exists = frappe.db.exists

		def exists(doctype, *args, **kwargs):
			return True if (doctype, args[:1]) == ("DocType", ("Bosta Shipment",)) else real_exists(doctype, *args, **kwargs)

		with patch.object(frappe.db, "exists", side_effect=exists), self.assertRaises(Exception) as caught:
			migration.execute()
		self.assertIn("table is missing", str(caught.exception))


class IntegrationTestCreateBostaTransporterSupplier(IntegrationTestCase):
	def test_the_supplier_exists_once_after_any_number_of_runs(self):
		create_bosta_transporter_supplier.execute()
		create_bosta_transporter_supplier.execute()
		suppliers = frappe.get_all("Supplier", filters={"supplier_name": "Bosta"}, fields=["name", "is_transporter"])
		self.assertEqual(len(suppliers), 1)
		self.assertTrue(suppliers[0].is_transporter)
