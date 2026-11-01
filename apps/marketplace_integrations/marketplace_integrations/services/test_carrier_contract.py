# Copyright (c) 2026, Tamwenat El Zekrayat and contributors
# For license information, please see license.txt

"""The neutral carrier contract (#415): every adapter in ``CARRIERS`` exposes every contract method, and the
neutral files hold no carrier name and no carrier-specific field."""

import inspect
import re
from pathlib import Path

from frappe.tests import IntegrationTestCase

from marketplace_integrations.services import carrier_shipments, marketplace_attention

APP = Path(__file__).resolve().parent.parent

CONTRACT_NAMES = (
	"SHIPPING_CARRIER",
	"SERVICE_NAME",
	"applies_to",
	"guard_changes",
	"clear_inherited",
	"validate_ready",
	"find_remote",
	"create_remote",
	"created_values",
	"log_data",
	"after_created",
	"cancel_remote",
	"after_cancelled",
	"refresh",
	"reconcile",
	"refuse_sales_order_cancel",
	"problem_states",
	"terminal_states",
)

#: Files that must stay carrier neutral. The registry import and the ``CARRIERS`` row are the only lines that
#: name an adapter.
NEUTRAL_FILES = (
	"services/carrier_shipments.py",
	"services/carrier_log.py",
	"api/carrier_shipments.py",
	"public/js/delivery_note_shipping.js",
)
REGISTRY_LINES = re.compile(r"^(from marketplace_integrations\.integrations\.\w+ import .*|CARRIERS = .*)$")
CARRIER_NAMES = re.compile(r"Bosta|Noon", re.IGNORECASE)
#: Fields that only one carrier uses.
CARRIER_FIELDS = re.compile(r"custom_bosta_|custom_noon_|custom_cod_")


class IntegrationTestCarrierContract(IntegrationTestCase):
	def test_every_adapter_exposes_every_contract_method(self):
		for name, adapter in carrier_shipments.CARRIERS.items():
			for attribute in CONTRACT_NAMES:
				self.assertTrue(hasattr(adapter, attribute), f"{name} adapter lacks {attribute}")

	def test_neutral_files_name_no_carrier_and_no_carrier_field(self):
		for relative in NEUTRAL_FILES:
			for number, line in enumerate((APP / relative).read_text().splitlines(), 1):
				if REGISTRY_LINES.match(line):
					continue
				self.assertIsNone(CARRIER_NAMES.search(line), f"{relative}:{number} names a carrier: {line}")
				self.assertIsNone(CARRIER_FIELDS.search(line), f"{relative}:{number} uses a carrier field: {line}")

	def test_needs_attention_carrier_collectors_are_neutral(self):
		# The rest of marketplace_attention.py is platform-specific; only the carrier collectors are neutral.
		for function in (marketplace_attention._collect_carrier_items, marketplace_attention._collect_carrier_return_reviews):
			source = inspect.getsource(function)
			self.assertIsNone(CARRIER_NAMES.search(source), f"{function.__name__} names a carrier")
			self.assertIsNone(CARRIER_FIELDS.search(source), f"{function.__name__} uses a carrier field")
