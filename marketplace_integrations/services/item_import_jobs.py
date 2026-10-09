# Copyright (c) 2026, Tamwenat El Zekrayat and contributors
# For license information, please see license.txt

"""Short-lived, user-owned background jobs for Item XLSX work."""

import frappe
from frappe import _

RESULT_TTL = 60 * 60
EVENT = "marketplace_item_import_progress"


def queue_export():
	return _queue("export", _run_export)


def queue_apply(file_url):
	return _queue("apply", _run_apply, file_url=file_url)


def get_status(job_id):
	state = frappe.cache().get_value(_key(job_id))
	if not state or state.get("owner") != frappe.session.user:
		frappe.throw(_("Item import job not found."), frappe.PermissionError)
	return {key: value for key, value in state.items() if key != "owner"}


def _queue(kind, method, **kwargs):
	job_id = frappe.generate_hash(length=16)
	owner = frappe.session.user
	_set(job_id, {"owner": owner, "kind": kind, "status": "queued", "progress": 0})
	frappe.enqueue(
		method,
		queue="long",
		timeout=3600,
		job_id=f"marketplace-item-import-{job_id}",
		job_id_for_result=job_id,
		owner=owner,
		kind=kind,
		**kwargs,
	)
	return {"job_id": job_id, "status": "queued"}


def _run_export(job_id_for_result, owner, kind):
	from marketplace_integrations.api import item_import

	try:
		_update(job_id_for_result, owner, kind, status="running", progress=10)
		content = item_import._export_items_xlsx_bytes()
		_update(job_id_for_result, owner, kind, progress=90)
		file_doc = frappe.get_doc(
			{
				"doctype": "File",
				"file_name": "items.xlsx",
				"content": content,
				"is_private": 1,
			}
		).insert()
		_update(
			job_id_for_result,
			owner,
			kind,
			status="completed",
			progress=100,
			result={"file_url": file_doc.file_url},
		)
	except Exception as exc:
		_update(job_id_for_result, owner, kind, status="failed", error=str(exc))
		raise


def _run_apply(job_id_for_result, owner, kind, file_url):
	from marketplace_integrations.api import item_import

	try:
		_update(job_id_for_result, owner, kind, status="running", progress=0)
		result = item_import._apply_items_import(
			file_url,
			progress_callback=lambda complete, total: _update_progress(
				job_id_for_result, owner, kind, complete, total
			),
		)
		_update(job_id_for_result, owner, kind, status="completed", progress=100, result=result)
	except Exception as exc:
		_update(job_id_for_result, owner, kind, status="failed", error=str(exc))
		raise


def _key(job_id):
	return f"marketplace_integrations:item_import_job:{job_id}"


def _set(job_id, state):
	frappe.cache().set_value(_key(job_id), state, expires_in_sec=RESULT_TTL)


def _update_progress(job_id, owner, kind, complete, total):
	progress = round(complete * 100 / total) if total else 100
	state = frappe.cache().get_value(_key(job_id))
	if state and state.get("progress") == progress:
		return
	_update(job_id, owner, kind, progress=progress)


def _update(job_id, owner, kind, **changes):
	state = frappe.cache().get_value(_key(job_id)) or {"owner": owner, "kind": kind}
	state.update(changes)
	_set(job_id, state)
	frappe.publish_realtime(
		EVENT,
		{"job_id": job_id, **{key: value for key, value in state.items() if key != "owner"}},
		user=owner,
	)
