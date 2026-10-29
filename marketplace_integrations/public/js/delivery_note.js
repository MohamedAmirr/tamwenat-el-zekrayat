// Copyright (c) 2026, Tamwenat El Zekrayat and contributors
// For license information, please see license.txt

// Delivery Note form script for Delivery Note-first Noon fulfillment (#391, epic #392).
// Saving a draft Delivery Note of a Noon Sales Order queues the Noon shipment on the server; this
// script only shows the result (custom_carrier_status, lr_no = AWB, custom_carrier_error) and the
// Retry button. Every rule is enforced server side (services/carrier_shipments.py).
//
// Wrapped in a function: doctype_js files share one scope with ERPNext's own Delivery Note script.
(function () {
	// A Pending note not modified for this long counts as stuck and can be retried. Mirrors
	// STALE_PENDING_MINUTES in integrations/noon/delivery_note_shipment.py (the server is the gate).
	const STALE_PENDING_MINUTES = 15;
	const API = "marketplace_integrations.api.noon_fulfillment.";
	const RETRY_METHOD = API + "retry_delivery_note_shipment";
	const POLL_MS = 2000;
	const NOON_GROUP = "Noon";

	function show_carrier_status(frm) {
		const status = frm.doc && frm.doc.custom_carrier_status;
		if (!status || frm.is_new()) return;
		if (status === "Pending") {
			frm.set_intro(__("Creating the Noon shipment..."), "blue");
		} else if (status === "Created") {
			const hint =
				frm.doc.docstatus === 2
					? ""
					: frm.doc.docstatus === 1
					? __("This Delivery Note cannot be cancelled until its Noon shipment is cancelled: use Cancel, which cancels the Noon shipment first.")
					: __("This Delivery Note cannot be cancelled or deleted until its Noon shipment is cancelled: use the Cancel Noon Shipment button, then delete it.");
			frm.set_intro(
				__("Noon shipment created. AWB: {0}", [frappe.utils.escape_html(frm.doc.lr_no || "")]) +
					(hint ? `<br>${hint}` : ""),
				"green"
			);
		} else if (status === "Cancelled") {
			frm.set_intro(
				__("The Noon shipment was cancelled. Use Retry Noon Shipment to create a new one, or delete this Delivery Note."),
				"yellow"
			);
		} else if (status === "Error") {
			// custom_carrier_error is stored already escaped by the server (it can hold Noon text).
			frm.set_intro(__("Noon shipment failed: {0}", [frm.doc.custom_carrier_error || ""]), "red");
		}
	}

	function is_stale_pending(frm) {
		if (frm.doc.custom_carrier_status !== "Pending" || !frm.doc.modified) return false;
		const age = moment(frappe.datetime.now_datetime()).diff(moment(frm.doc.modified), "minutes");
		return age >= STALE_PENDING_MINUTES;
	}

	function add_retry_button(frm) {
		if (!frm.doc || frm.is_new() || frm.doc.docstatus !== 0) return;
		if (!frappe.model.can_write("Delivery Note")) return;
		if (!["Error", "Cancelled"].includes(frm.doc.custom_carrier_status) && !is_stale_pending(frm)) return;
		frm.add_custom_button(__("Retry Noon Shipment"), () => {
			frappe
				.call({
					method: RETRY_METHOD,
					type: "POST",
					args: { delivery_note: frm.doc.name },
					freeze: true,
					freeze_message: __("Retrying the Noon shipment..."),
				})
				.then(() => frm.reload_doc());
		});
	}


	// ---- Cancel / delete: the Noon shipment is cancelled first (#393) -------------------------

	function is_noon_note(frm) {
		return Boolean(frm.doc && !frm.is_new() && frm.doc.custom_carrier_status);
	}

	function server_message(error) {
		// responseJSON._server_messages is a JSON array of JSON strings (see docs/frappe-conventions.md).
		try {
			const messages = JSON.parse(error.responseJSON._server_messages || "[]");
			// parsed in an inert document: the message is server HTML and is never put into the page
			return new DOMParser().parseFromString(JSON.parse(messages[0]).message, "text/html").body.textContent;
		} catch (e) {
			return "";
		}
	}

	// Cancels the Noon shipment of the note and resolves true once nothing live is left. A
	// refusal or failure resolves false and shows Noon's message in the dialog; nothing changes.
	function cancel_noon_shipment(frm) {
		return new Promise((resolve) => {
			let dialog = null;
			let timer = null;
			let settled = false;
			const finish = (ok) => {
				if (settled) return;
				settled = true;
				if (timer) clearTimeout(timer);
				if (dialog) dialog.hide();
				resolve(ok);
			};
			const show = (title, html, with_close) => {
				if (!dialog) {
					dialog = new frappe.ui.Dialog({
						title: __("Noon shipment"),
						fields: [{ fieldtype: "HTML", fieldname: "body" }],
						primary_action_label: __("Close"),
						primary_action: () => finish(false),
						// Closing the dialog (X, Escape) abandons the flow: the note is left unchanged.
						on_hide: () => finish(false),
					});
					dialog.show();
				}
				dialog.set_title(title);
				dialog.fields_dict.body.$wrapper.html(html);
				dialog.get_primary_btn().toggle(Boolean(with_close));
			};
			const handle = (state) => {
				if (state.state === "done") return finish(true);
				if (state.state === "error") {
					show(
						__("Noon refused to cancel the shipment"),
						`<p>${frappe.utils.escape_html(state.message || "")}</p>` +
							`<p class="text-muted">${__("The Delivery Note was not changed.")}</p>`,
						true
					);
					return;
				}
				if (state.state === "live") {
					// Nothing is cancelling it: do not poll forever.
					show(
						__("The Noon shipment was not cancelled"),
						`<p>${__("The Noon shipment is still live and no cancel is running. Close this and try again.")}</p>`,
						true
					);
					return;
				}
				show(
					__("Cancelling the Noon shipment..."),
					`<p>${__("Please wait. The Delivery Note will continue once Noon has cancelled the shipment.")}</p>` +
						`<p class="text-warning">${__("Closing this dialog or reloading the page does not stop the Noon cancel once it has started. The Delivery Note itself is then still not cancelled: open it and cancel it again.")}</p>`,
					false
				);
				timer = setTimeout(poll, POLL_MS);
			};
			const poll = () =>
				frappe
					.call({ method: API + "get_delivery_note_cancel_state", args: { delivery_note: frm.doc.name }, silent: true })
					.then((r) => handle(r.message || {}), () => finish(false));
			frappe
				.call({
					method: API + "cancel_delivery_note_shipment",
					args: { delivery_note: frm.doc.name },
					type: "POST",
					silent: true, // the error is shown once, below
				})
				.then((r) => handle(r.message || {}), (error) => {
					const message = server_message(error);
					if (message) frappe.msgprint({ message: frappe.utils.escape_html(message), indicator: "red" });
					finish(false);
				});
		});
	}

	// Cancel (submitted note): before_cancel runs after the native confirmation and honours
	// frappe.validated, so the form's own cancel continues only when the shipment is cancelled.
	function before_cancel(frm) {
		if (!is_noon_note(frm)) return;
		return cancel_noon_shipment(frm).then((ok) => {
			if (!ok) frappe.validated = false;
		});
	}

	// Delete (draft note): the form has no event before Delete, so a draft gets this button; the
	// server refuses the delete while the shipment is live and its message points here.
	function add_cancel_shipment_button(frm) {
		if (!is_noon_note(frm) || frm.doc.docstatus !== 0 || !frappe.model.can_delete("Delivery Note")) return;
		if (!["Created", "Pending", "Error"].includes(frm.doc.custom_carrier_status)) return;
		frm.add_custom_button(
			__("Cancel Noon Shipment"),
			() =>
				cancel_noon_shipment(frm).then((ok) => {
					if (!ok) return;
					frappe.show_alert({
						message: __("The Noon shipment is cancelled. You can now delete this Delivery Note."),
						indicator: "green",
					});
					frm.reload_doc();
				}),
			NOON_GROUP
		);
	}

	// ---- Add AWB, shipment invoice, Retry on the note (#393) --------------------------------------

	function queued_alert(r, frm) {
		const queued = (r.message || {}).queued;
		frappe.show_alert({
			message: queued
				? __("Noon fulfillment action queued. Refresh this document to see the results.")
				: __("A Noon fulfillment action for this shipment is already queued or running."),
			indicator: "blue",
		});
		frm.reload_doc();
	}

	function show_add_awb_dialog(frm) {
		const dialog = new frappe.ui.Dialog({
			title: __("Add Noon Shipment AWB"),
			fields: [{ fieldname: "awb_nr", fieldtype: "Data", label: __("AWB Nr"), reqd: 1 }],
			primary_action_label: __("Add AWB"),
			primary_action(values) {
				dialog.hide();
				frappe
					.call({
						method: API + "add_delivery_note_awb",
						args: { delivery_note: frm.doc.name, awb_nr: values.awb_nr },
						freeze: true,
						freeze_message: __("Queuing AWB addition..."),
					})
					.then((r) => queued_alert(r, frm));
			},
		});
		dialog.show();
	}

	function show_upload_invoice_dialog(frm) {
		const dialog = new frappe.ui.Dialog({
			title: __("Upload Noon Shipment Invoice"),
			fields: [
				{
					fieldname: "invoice_file",
					fieldtype: "Attach",
					label: __("Invoice PDF"),
					reqd: 1,
					description: __("Must be uploaded as Private - a public file is rejected."),
				},
				{ fieldname: "invoice_nr", fieldtype: "Data", label: __("Invoice Nr"), reqd: 1 },
				{ fieldname: "issued_at", fieldtype: "Date", label: __("Issued At"), reqd: 1 },
			],
			primary_action_label: __("Upload"),
			primary_action(values) {
				dialog.hide();
				frappe
					.call({
						method: API + "upload_delivery_note_invoice",
						args: {
							delivery_note: frm.doc.name,
							file_url: values.invoice_file,
							invoice_nr: values.invoice_nr,
							issued_at: values.issued_at,
						},
						freeze: true,
						freeze_message: __("Queuing invoice upload..."),
					})
					.then((r) => queued_alert(r, frm));
			},
		});
		dialog.show();
	}

	function run_retry_awb_or_invoice(frm) {
		frappe
			.call({
				method: API + "retry_delivery_note_awb_or_invoice",
				args: { delivery_note: frm.doc.name },
				freeze: true,
				freeze_message: __("Queuing retry..."),
			})
			.then((r) => queued_alert(r, frm));
	}

	// Error only: the failed shipment is dropped locally (no Noon call) so the note can get a new one or be deleted.
	function run_discard_shipment(frm) {
		frappe.confirm(
			__("Discard the failed Noon shipment? Nothing is sent to Noon: if Noon created it anyway it keeps existing there. Then use Retry Noon Shipment or delete this Delivery Note."),
			() =>
				frappe
					.call({
						method: API + "discard_delivery_note_shipment",
						args: { delivery_note: frm.doc.name },
						freeze: true,
						freeze_message: __("Discarding the Noon shipment..."),
					})
					.then(() => frm.reload_doc())
		);
	}

	// Pure: which note buttons a shipment summary (get_delivery_note_shipment) allows.
	function note_shipment_actions(summary) {
		if (summary && summary.status === "Error") return ["discard"];
		if (!summary || summary.status !== "Created") return [];
		const actions = ["add_awb"];
		if (summary.invoice_status !== "Pending") actions.push("upload_invoice");
		const pending_awb = (summary.awbs || []).some((awb) => awb.status === "Pending");
		if (pending_awb || ["Pending", "Error"].includes(summary.invoice_status)) actions.push("retry");
		return actions;
	}

	function add_shipment_buttons(frm) {
		if (!is_noon_note(frm) || !frappe.model.can_write("Delivery Note")) return;
		if (!["Created", "Error"].includes(frm.doc.custom_carrier_status)) return;
		const request = (frm.__mi_shipment_request = (frm.__mi_shipment_request || 0) + 1);
		frappe
			.call({ method: API + "get_delivery_note_shipment", args: { delivery_note: frm.doc.name }, type: "POST", silent: true })
			.then((r) => {
				if (request !== frm.__mi_shipment_request) return;
				const actions = note_shipment_actions(r.message);
				const buttons = {
					add_awb: [__("Add AWB"), () => show_add_awb_dialog(frm)],
					upload_invoice: [__("Upload Shipment Invoice"), () => show_upload_invoice_dialog(frm)],
					retry: [__("Retry AWB or Invoice"), () => run_retry_awb_or_invoice(frm)],
					discard: [__("Discard Noon Shipment"), () => run_discard_shipment(frm)],
				};
				actions.forEach((action) => frm.add_custom_button(buttons[action][0], buttons[action][1], NOON_GROUP));
			});
	}

	if (typeof frappe !== "undefined" && frappe.ui && frappe.ui.form) {
		frappe.ui.form.on("Delivery Note", {
			refresh(frm) {
				show_carrier_status(frm);
				add_retry_button(frm);
				add_cancel_shipment_button(frm);
				add_shipment_buttons(frm);
			},
			before_cancel,
		});
	}

	if (typeof module !== "undefined") {
		module.exports = {
			show_carrier_status,
			add_retry_button,
			is_stale_pending,
			cancel_noon_shipment,
			before_cancel,
			add_cancel_shipment_button,
			note_shipment_actions,
		};
	}
})();
