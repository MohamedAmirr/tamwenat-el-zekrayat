# Copyright (c) 2026, Tamwenat El Zekrayat and contributors
# For license information, please see license.txt

"""Pre-generated Noon Logistics AWB pool.

``generate`` calls ``GetNoonLogisticsAWBs`` once for a requested quantity and
stores every returned AWB as an ``Available`` ``Noon AWB`` row.
``claim_pool_awb()`` is used by ``fulfillment.run_create_shipment()`` to take
one Available AWB for a shipment instead of fetching a single AWB from Noon.

A claimed AWB never returns to the pool: a discarded, cancelled or failed
shipment keeps consuming the AWB it claimed.
"""

import frappe
from frappe import _
from frappe.utils import cint, cstr, now_datetime

from marketplace_integrations.integrations.noon.catalog import get_account, validate_connection_enabled
from marketplace_integrations.integrations.noon.client import NoonClient
from marketplace_integrations.integrations.noon.constants import NOON_LOGISTICS_AWBS_GET_PATH

MIN_QTY = 1
MAX_QTY = 1000
RQ_JOB_PREFIX = "marketplace-noon-awb-pool"
_CLAIM_ATTEMPTS = 3


def validate_qty(qty):
	text = cstr(qty).strip()
	if not text.isdigit() or not MIN_QTY <= int(text) <= MAX_QTY:
		frappe.throw(_("Quantity must be a whole number between {0} and {1}.").format(MIN_QTY, MAX_QTY))
	return int(text)


def count_available(account_name):
	return frappe.db.count("Noon AWB", {"marketplace_account": account_name, "status": "Available"})


def queue_generate(account_name, qty):
	"""Validate and queue AWB generation on the ``long`` queue (no Noon HTTP
	call in the Desk request), deduplicated per account."""
	account = get_account(account_name)
	account.check_permission("write")
	validate_connection_enabled(account)
	qty = validate_qty(qty)
	if not cstr(account.noon_country_code).strip():
		frappe.throw(_("Marketplace Account {0} requires a Noon country code.").format(account.name))
	job = frappe.enqueue(
		run_generate,
		queue="long",
		timeout=600,
		job_id=f"{RQ_JOB_PREFIX}-{account.name}",
		deduplicate=True,
		account_name=account.name,
		qty=qty,
		requested_by=frappe.session.user,
	)
	return {"queued": job is not None}


def run_generate(account_name, qty, requested_by=None):
	"""Worker target. Tells the requesting user the outcome via a realtime
	message, since nothing else surfaces a background result."""
	frappe.set_user("Administrator")
	try:
		stored, skipped = generate(account_name, qty)
	except Exception as exc:
		frappe.log_error(title="Noon AWB generation failed", message=frappe.get_traceback())
		_notify(requested_by, _("Noon AWB generation failed: {0}").format(cstr(exc)), "red")
		return
	message = _("Stored {0} new Noon AWB(s).").format(stored)
	if skipped:
		message += " " + _("{0} already existed and were skipped.").format(skipped)
	_notify(requested_by, message, "green")


def _notify(user, message, indicator):
	if user:
		frappe.publish_realtime(
			"msgprint", {"message": message, "title": _("Noon AWBs"), "indicator": indicator}, user=user
		)


def generate(account_name, qty):
	"""Call GetNoonLogisticsAWBs once and store each AWB as Available.
	Returns ``(stored, skipped_existing)``."""
	account = get_account(account_name)
	validate_connection_enabled(account)
	qty = validate_qty(qty)
	country_code = cstr(account.noon_country_code).strip()
	if not country_code:
		frappe.throw(_("Marketplace Account {0} requires a Noon country code.").format(account.name))

	client = NoonClient(account.connection)
	response = client.request(
		"POST", NOON_LOGISTICS_AWBS_GET_PATH, json={"country_code": country_code, "qty": qty}
	)
	if response.status_code != 200:
		frappe.throw(client.error_message(response))
	try:
		awb_nrs = (response.json() or {}).get("awb_nrs") or []
	except ValueError:
		awb_nrs = []
	if not awb_nrs:
		frappe.throw(_("Noon returned no AWB number."))

	from marketplace_integrations.marketplace_integrations.doctype.noon_awb.noon_awb import (
		compute_awb_identity_key,
	)

	stored = skipped = 0
	generated_on = now_datetime()
	for awb_nr in awb_nrs:
		awb_nr = cstr(awb_nr).strip()
		if not awb_nr:
			continue
		if frappe.db.exists("Noon AWB", {"awb_identity_key": compute_awb_identity_key(account.name, awb_nr)}):
			skipped += 1
			continue
		frappe.get_doc(
			{
				"doctype": "Noon AWB",
				"marketplace_account": account.name,
				"awb_nr": awb_nr,
				"country_code": country_code,
				"status": "Available",
				"generated_on": generated_on,
			}
		).insert(ignore_permissions=True)
		stored += 1
	frappe.db.commit()
	return stored, skipped


def claim_pool_awb(account_name, country_code, marketplace_order, integration_shipment_nr):
	"""Atomically claim one Available AWB for a shipment and return its AWB
	Nr, or ``None`` when the pool is empty.

	Must be called with no open transaction state worth keeping (the caller
	commits first, releasing the order lock, as it does before any Noon HTTP
	call): it commits on success and rolls back on a concurrency error.
	The claim is ONE ``UPDATE ... WHERE status = 'Available' ... LIMIT 1``, so
	two workers can never take the same row; the row records the order and
	shipment, so a repeat call for the same shipment (e.g. a worker that died
	after claiming) returns the AWB already claimed instead of consuming
	another.
	"""
	country_code = cstr(country_code).strip()
	for _attempt in range(_CLAIM_ATTEMPTS):
		try:
			existing = frappe.db.get_value(
				"Noon AWB",
				{
					"marketplace_account": account_name,
					"marketplace_order": marketplace_order,
					"integration_shipment_nr": integration_shipment_nr,
					"status": "Used",
				},
				"awb_nr",
			)
			if existing:
				frappe.db.commit()
				return existing
			now = now_datetime()
			frappe.db.sql(
				"""
				UPDATE `tabNoon AWB`
				SET status = 'Used', marketplace_order = %s, integration_shipment_nr = %s,
					used_on = %s, modified = %s
				WHERE marketplace_account = %s AND country_code = %s AND status = 'Available'
				ORDER BY creation, name
				LIMIT 1
				""",
				(marketplace_order, integration_shipment_nr, now, now, account_name, country_code),
			)
			claimed = frappe.db._cursor.rowcount == 1
			awb_nr = None
			if claimed:
				awb_nr = frappe.db.get_value(
					"Noon AWB",
					{
						"marketplace_account": account_name,
						"marketplace_order": marketplace_order,
						"integration_shipment_nr": integration_shipment_nr,
						"status": "Used",
					},
					"awb_nr",
				)
			frappe.db.commit()
			return awb_nr
		except frappe.QueryDeadlockError:
			# MariaDB snapshot isolation (1020) or a deadlock: another worker
			# touched the row; nothing of ours is pending, so retry fresh.
			frappe.db.rollback()
	return None
