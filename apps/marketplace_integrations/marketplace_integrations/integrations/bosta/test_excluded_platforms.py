# Copyright (c) 2026, Tamwenat El Zekrayat and contributors
# For license information, please see license.txt

"""Bosta is not offered for Sales Orders of platforms that fulfil their own orders (#368)."""

import json
import re
from types import SimpleNamespace
from unittest.mock import patch

import frappe
from frappe.tests import UnitTestCase

from marketplace_integrations.integrations.bosta import bulk, deliveries
from marketplace_integrations.integrations.bosta.constants import BOSTA_EXCLUDED_PLATFORMS

DELIVERIES = "marketplace_integrations.integrations.bosta.deliveries"


class _PlatformOrder(SimpleNamespace):
	def get(self, key, default=None):
		return getattr(self, key, default)


def make_order(platform=None):
	return _PlatformOrder(
		name="SAL-ORD-1",
		docstatus=1,
		status="To Deliver and Bill",
		custom_marketplace_order="MKTORD-1" if platform else None,
		custom_marketplace_platform=platform,
		items=[SimpleNamespace(delivered_qty=0, qty=1, delivered_by_supplier=0)],
	)


def _platform_lookup(platform):
	def get_value(doctype, name=None, fieldname=None, *args, **kwargs):
		assert doctype == "Marketplace Order"
		return platform

	return patch.object(frappe.db, "get_value", side_effect=get_value)


class UnitTestBostaExcludedPlatforms(UnitTestCase):
	def test_the_platform_list_is_noon_and_talabat_only(self):
		# Amazon and Instashop are added when those platforms are built, not before.
		self.assertEqual(BOSTA_EXCLUDED_PLATFORMS, ("Noon", "Talabat"))

	def test_python_and_js_lists_are_the_same(self):
		path = frappe.get_app_path("marketplace_integrations", "public", "js", "sales_order.js")
		with open(path) as handle:
			match = re.search(r"const BOSTA_EXCLUDED_PLATFORMS = (\[[^\]]*\]);", handle.read())
		self.assertIsNotNone(match, "sales_order.js must define BOSTA_EXCLUDED_PLATFORMS")
		self.assertEqual(tuple(json.loads(match.group(1))), BOSTA_EXCLUDED_PLATFORMS)

	def test_excluded_platform_reads_the_linked_marketplace_order(self):
		for platform, expected in (("Noon", "Noon"), ("Talabat", "Talabat"), ("Shopify", None)):
			with self.subTest(platform=platform), _platform_lookup(platform):
				self.assertEqual(deliveries.excluded_platform(make_order(platform)), expected)

	def test_a_sales_order_without_a_platform_keeps_bosta(self):
		self.assertIsNone(deliveries.excluded_platform(make_order(None)))

	def test_validate_deliverable_refuses_noon_and_talabat_with_a_message_naming_the_platform(self):
		for platform in ("Noon", "Talabat"):
			with self.subTest(platform=platform), _platform_lookup(platform):
				with self.assertRaises(frappe.ValidationError) as caught:
					deliveries._validate_deliverable(make_order(platform))
				self.assertIn(platform, str(caught.exception))
				self.assertIn("fulfils its own orders", str(caught.exception))

	def test_shopify_and_unlinked_orders_pass_the_platform_check(self):
		with _platform_lookup("Shopify"):
			deliveries._validate_deliverable(make_order("Shopify"))
		deliveries._validate_deliverable(make_order(None))

	def test_defaults_single_create_and_bulk_plan_all_refuse_an_excluded_order(self):
		order = make_order("Noon")
		with (
			_platform_lookup("Noon"),
			patch(f"{DELIVERIES}._require_creator_role"),
			patch(f"{DELIVERIES}._get_sales_order", return_value=order),
			patch(f"{DELIVERIES}._active_shipment", return_value=None),
			patch(f"{DELIVERIES}.lock_and_load_sales_order", return_value=order),
			patch(f"{DELIVERIES}.locations.get_city", return_value={"id": "c", "name": "n", "code": "x"}),
			patch(f"{DELIVERIES}.BostaClient") as bosta_client,
		):
			with self.assertRaises(frappe.ValidationError):
				deliveries.get_sales_order_defaults("SAL-ORD-1")
			with self.assertRaises(frappe.ValidationError):
				deliveries.create_bosta_shipment("SAL-ORD-1", "Name", "0100", "c", "Street 1")
			plan = bulk.get_bulk_create_plan(frappe.as_json(["SAL-ORD-1"]))
		bosta_client.assert_not_called()  # nothing reached Bosta
		self.assertEqual(plan["ready"], [])
		self.assertEqual(plan["review"], [])
		self.assertEqual([row["sales_order"] for row in plan["skipped"]], ["SAL-ORD-1"])
		self.assertIn("fulfils its own orders", plan["skipped"][0]["reason"])
