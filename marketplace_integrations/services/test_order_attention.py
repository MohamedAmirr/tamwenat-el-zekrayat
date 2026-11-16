# Copyright (c) 2026, Tamwenat El Zekrayat and contributors
# For license information, please see license.txt

"""The Needs Attention flag of a Sales Order (#533): the service helpers, the hook that keeps the stored value, and the
"Mark as Resolved" API."""

from unittest.mock import patch

import frappe
from frappe.tests import IntegrationTestCase
from frappe.utils import add_days, nowdate

from marketplace_integrations.api import order_attention as api
from marketplace_integrations.services import order_attention as service
from marketplace_integrations.tests.utils import make_role_user


class IntegrationTestOrderAttention(IntegrationTestCase):
	def setUp(self):
		self.suffix = frappe.generate_hash(length=8)
		self.company = frappe.defaults.get_global_default("company")
		self.item = frappe.get_doc(
			{
				"doctype": "Item",
				"item_code": f"TEST-ATTN-{self.suffix}",
				"item_group": "Products",
				"stock_uom": "Nos",
				"is_stock_item": 0,
			}
		).insert()
		self.customer = frappe.get_doc(
			{
				"doctype": "Customer",
				"customer_name": f"Attention Customer {self.suffix}",
				"customer_group": frappe.db.get_value("Customer Group", {"is_group": 0}, "name"),
				"territory": frappe.db.get_value("Territory", {"is_group": 0}, "name"),
			}
		).insert()

	def _order(self, submit=True):
		order = frappe.get_doc(
			{
				"doctype": "Sales Order",
				"company": self.company,
				"customer": self.customer.name,
				"transaction_date": nowdate(),
				"delivery_date": add_days(nowdate(), 1),
				"items": [{"item_code": self.item.name, "qty": 1, "rate": 10, "delivery_date": add_days(nowdate(), 1)}],
			}
		).insert()
		if submit:
			order.submit()
		return order.name

	def _state(self, name):
		return frappe.db.get_value(
			"Sales Order", name, [service.FLAG_FIELD, service.REASON_FIELD], as_dict=True
		)

	def _comments(self, name):
		return frappe.get_all(
			"Comment",
			filters={"reference_doctype": "Sales Order", "reference_name": name, "comment_type": "Comment"},
			pluck="content",
			order_by="creation asc",
		)

	# --- flag_attention ---------------------------------------------------------------

	def test_flags_a_submitted_order(self):
		name = self._order()
		modified = frappe.db.get_value("Sales Order", name, "modified")

		self.assertTrue(service.flag_attention(name, "Check the shipping."))

		state = self._state(name)
		self.assertEqual((state[service.FLAG_FIELD], state[service.REASON_FIELD]), (1, "Check the shipping."))
		self.assertEqual(frappe.db.get_value("Sales Order", name, "modified"), modified)

	def test_a_draft_order_is_not_flagged(self):
		name = self._order(submit=False)

		self.assertFalse(service.flag_attention(name, "Check the shipping."))

		self.assertEqual(self._state(name)[service.FLAG_FIELD], 0)
		self.assertEqual(self._comments(name), [])

	def test_a_cancelled_order_is_not_flagged(self):
		name = self._order()
		frappe.get_doc("Sales Order", name).cancel()

		self.assertFalse(service.flag_attention(name, "Check the shipping."))

		self.assertEqual(self._state(name)[service.FLAG_FIELD], 0)

	def test_the_reason_is_plain_text_on_one_line(self):
		name = self._order()

		service.flag_attention(name, "  <b>Refund</b>   owed:\n\n<script>alert(1)</script>  10 <EGP>\t now ")

		reason = self._state(name)[service.REASON_FIELD]
		self.assertNotIn("<", reason)
		self.assertNotIn(">", reason)
		self.assertNotIn("\n", reason)
		self.assertNotIn("  ", reason)
		self.assertTrue(reason.startswith("Refund owed:"))
		self.assertEqual(reason, reason.strip())

	def test_clean_reason_collapses_whitespace_and_strips_tags(self):
		self.assertEqual(service.clean_reason("<p>a  b</p>\n c"), "a b c")
		self.assertEqual(service.clean_reason(None), "")
		self.assertEqual(service.clean_reason("<br>"), "")

	def test_the_same_reason_twice_changes_nothing_and_adds_no_second_comment(self):
		name = self._order()
		self.assertTrue(service.flag_attention(name, "Same problem."))

		self.assertFalse(service.flag_attention(name, "Same problem."))
		# The same text with different markup is the same reason once cleaned.
		self.assertFalse(service.flag_attention(name, "  <b>Same</b> problem. "))

		self.assertEqual(self._state(name)[service.REASON_FIELD], "Same problem.")
		self.assertEqual(len(self._comments(name)), 1)

	def test_a_different_reason_is_appended_on_a_new_line(self):
		name = self._order()
		service.flag_attention(name, "First problem.")

		self.assertTrue(service.flag_attention(name, "Second problem."))

		self.assertEqual(self._state(name)[service.REASON_FIELD], "First problem.\nSecond problem.")
		self.assertEqual(len(self._comments(name)), 2)
		# a repeat of the FIRST entry is still a no-op
		self.assertFalse(service.flag_attention(name, "First problem."))

	def test_an_empty_reason_throws(self):
		name = self._order()

		for reason in ("", "   ", None, "<br>"):
			with self.subTest(reason=reason), self.assertRaises(frappe.ValidationError):
				service.flag_attention(name, reason)

		self.assertEqual(self._state(name)[service.FLAG_FIELD], 0)

	def test_a_missing_order_is_logged_and_returns_false(self):
		with patch.object(frappe, "log_error") as log:
			self.assertFalse(service.flag_attention("SO-DOES-NOT-EXIST", "Anything."))

		log.assert_called_once()
		self.assertIn("SO-DOES-NOT-EXIST", log.call_args.args[0])

	def test_the_timeline_comment_holds_the_escaped_reason(self):
		name = self._order()

		service.flag_attention(name, "Refund owed: 10 & more")

		self.assertEqual(self._comments(name), ["Needs Attention: Refund owed: 10 &amp; more"])

	# --- keep_stored_attention --------------------------------------------------------

	def test_a_stale_copy_saved_after_a_flag_does_not_wipe_it(self):
		name = self._order()
		stale = frappe.get_doc("Sales Order", name)
		service.flag_attention(name, "Real problem.")

		stale.flags.ignore_validate_update_after_submit = True
		stale.save()

		state = self._state(name)
		self.assertEqual((state[service.FLAG_FIELD], state[service.REASON_FIELD]), (1, "Real problem."))

	def test_a_form_save_cannot_set_the_flag_or_the_reason(self):
		name = self._order()
		doc = frappe.get_doc("Sales Order", name)
		doc.set(service.FLAG_FIELD, 1)
		doc.set(service.REASON_FIELD, "Made up on the form.")

		doc.flags.ignore_validate_update_after_submit = True
		doc.save()

		state = self._state(name)
		self.assertEqual((state[service.FLAG_FIELD], state[service.REASON_FIELD]), (0, ""))

	def test_a_stale_copy_saved_after_the_flag_was_cleared_does_not_bring_it_back(self):
		name = self._order()
		service.flag_attention(name, "Problem.")
		stale = frappe.get_doc("Sales Order", name)
		service.clear_attention(name)

		stale.flags.ignore_validate_update_after_submit = True
		stale.save()

		self.assertEqual(self._state(name)[service.FLAG_FIELD], 0)

	def test_a_new_order_starts_unflagged_even_when_the_values_are_set(self):
		doc = frappe.get_doc(
			{
				"doctype": "Sales Order",
				"company": self.company,
				"customer": self.customer.name,
				"transaction_date": nowdate(),
				"delivery_date": add_days(nowdate(), 1),
				service.FLAG_FIELD: 1,
				service.REASON_FIELD: "Copied from another order.",
				"items": [{"item_code": self.item.name, "qty": 1, "rate": 10, "delivery_date": add_days(nowdate(), 1)}],
			}
		).insert()

		state = self._state(doc.name)
		self.assertEqual((state[service.FLAG_FIELD], state[service.REASON_FIELD]), (0, ""))

	def test_the_helpers_own_writes_pass_the_hook(self):
		name = self._order()
		service.flag_attention(name, "One.")
		service.flag_attention(name, "Two.")
		self.assertEqual(self._state(name)[service.REASON_FIELD], "One.\nTwo.")

		self.assertEqual(service.clear_attention(name), "One.\nTwo.")

		self.assertEqual(self._state(name)[service.FLAG_FIELD], 0)
		self.assertFalse(frappe.flags.get(service._WRITE_MARKER))

	# --- clear_attention --------------------------------------------------------------

	def test_clear_on_an_unflagged_or_missing_order_does_nothing(self):
		name = self._order()

		self.assertIsNone(service.clear_attention(name))
		self.assertIsNone(service.clear_attention("SO-DOES-NOT-EXIST"))
		self.assertEqual(self._comments(name), [])

	# --- mark_resolved ----------------------------------------------------------------

	def test_mark_resolved_clears_both_fields_and_logs_the_old_reason(self):
		name = self._order()
		service.flag_attention(name, "Refund owed: 10.")

		self.assertEqual(api.mark_resolved(name), {"cleared": True})

		state = self._state(name)
		self.assertEqual((state[service.FLAG_FIELD], state[service.REASON_FIELD]), (0, ""))
		self.assertIn("Needs Attention resolved. The reason was: Refund owed: 10.", self._comments(name))

	def test_mark_resolved_twice_reports_nothing_cleared_the_second_time(self):
		name = self._order()
		service.flag_attention(name, "Problem.")
		api.mark_resolved(name)

		self.assertEqual(api.mark_resolved(name), {"cleared": False})

	def test_mark_resolved_refuses_a_draft(self):
		name = self._order(submit=False)

		with self.assertRaises(frappe.ValidationError):
			api.mark_resolved(name)

	def test_mark_resolved_works_on_a_cancelled_order_for_administrator(self):
		name = self._order()
		service.flag_attention(name, "Problem.")
		frappe.get_doc("Sales Order", name).cancel()
		# cancelling keeps the stored flag (the form copy was loaded after it)
		self.assertEqual(self._state(name)[service.FLAG_FIELD], 1)

		self.assertEqual(api.mark_resolved(name), {"cleared": True})

		self.assertEqual(self._state(name)[service.FLAG_FIELD], 0)

	def test_a_user_with_write_permission_can_resolve_a_submitted_and_a_cancelled_order(self):
		email, _role = make_role_user(self, "Attention Writer", {"Sales Order": ["read", "write"]})
		submitted, cancelled = self._order(), self._order()
		for name in (submitted, cancelled):
			service.flag_attention(name, "Problem.")
		frappe.get_doc("Sales Order", cancelled).cancel()

		# The answer to "does has_permission write pass on a cancelled order in Frappe 16" is recorded here.
		write_on_cancelled = frappe.has_permission("Sales Order", "write", doc=cancelled, user=email)
		write_on_submitted = frappe.has_permission("Sales Order", "write", doc=submitted, user=email)
		print(f"\nhas_permission(write): submitted={write_on_submitted} cancelled={write_on_cancelled}")
		self.assertTrue(write_on_submitted)

		frappe.set_user(email)
		self.addCleanup(frappe.set_user, "Administrator")
		self.assertEqual(api.mark_resolved(submitted), {"cleared": True})
		self.assertEqual(api.mark_resolved(cancelled), {"cleared": True})

	def test_a_user_without_write_permission_cannot_resolve(self):
		email, _role = make_role_user(self, "Attention Reader", {"Sales Order": ["read"]})
		name = self._order()
		service.flag_attention(name, "Problem.")

		frappe.set_user(email)
		self.addCleanup(frappe.set_user, "Administrator")
		with self.assertRaises(frappe.PermissionError):
			api.mark_resolved(name)

		frappe.set_user("Administrator")
		self.assertEqual(self._state(name)[service.FLAG_FIELD], 1)
