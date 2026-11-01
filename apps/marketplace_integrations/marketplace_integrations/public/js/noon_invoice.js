// Copyright (c) 2026, Tamwenat El Zekrayat and contributors
// For license information, please see license.txt

// The invoice upload dialog shared by "Upload Shipment Invoice" (Delivery Note form, delivery_note_noon.js)
// and "Upload Order Invoice" (Sales Order form, noon_fulfillment.js), #416. The PDF is attached by the
// person: Attach (PDF) + invoice number + issue date. Flow: a warning when an invoice was already uploaded
// (the new one replaces it at Noon), the form, "Sending to Noon..." while the job runs (polls
// api.noon_fulfillment.check_invoice_upload), then a small alert, or an error dialog with Retry. Retry sends
// the same idempotency key, so Noon sees the same upload. A failure never blocks anything else: the form
// stays as it was. Every text that came from the server or Noon is escaped where shown.
//
// Wrapped in a function: doctype_js files share one scope with ERPNext's own scripts.
(function () {
	const API = "marketplace_integrations.api.noon_fulfillment.";
	const timing = { poll_ms: 2000, max_wait_ms: 60000 };

	// The message of a rejected frappe.call(): `_server_messages` is a JSON array of JSON strings (see
	// docs/frappe-conventions.md). Parsed in an inert document; the plain text is escaped where shown.
	function server_message(error) {
		try {
			const messages = JSON.parse(error.responseJSON._server_messages || "[]");
			const texts = messages
				.map((entry) =>
					new DOMParser().parseFromString(JSON.parse(entry).message || "", "text/html").body.textContent.trim()
				)
				.filter(Boolean);
			if (texts.length) return texts.join(" ");
		} catch (e) {
			// fall through to the generic text
		}
		return "";
	}

	// options = {
	//   doctype, name        the document the button was used on ("Delivery Note" or "Sales Order")
	//   title                dialog title
	//   uploaded_on          when an invoice was uploaded before (shown in the intro), else empty
	//   uploaded             true when an invoice was uploaded before (a warning comes first)
	//   send(values, key)    frappe.call of the upload (key empty) or of its Retry (the key of the press)
	//   reload()             refreshes the form
	// }
	function start(options) {
		if (options.uploaded || options.uploaded_on) {
			const when = options.uploaded_on
				? __("An invoice was already uploaded on {0}.", [frappe.utils.escape_html(String(options.uploaded_on))])
				: __("An invoice was already uploaded.");
			frappe.confirm(
				`${when} ${frappe.utils.escape_html(__("The new upload replaces it at Noon. Continue?"))}`,
				() => show_form(options)
			);
			return;
		}
		show_form(options);
	}

	function show_form(options) {
		const dialog = new frappe.ui.Dialog({
			title: options.title,
			fields: [
				{
					fieldname: "invoice_file",
					fieldtype: "Attach",
					label: __("Invoice PDF"),
					reqd: 1,
					// The server rejects a non-private File; this just tells the user up front.
					description: __("Must be uploaded as Private - a public file is rejected."),
				},
				{ fieldname: "invoice_nr", fieldtype: "Data", label: __("Invoice Nr"), reqd: 1 },
				{ fieldname: "issued_at", fieldtype: "Date", label: __("Issued At"), reqd: 1 },
			],
			primary_action_label: __("Upload"),
			primary_action(values) {
				dialog.hide();
				send(options, values, "");
			},
		});
		dialog.show();
		return dialog;
	}

	function show_problem(options, values, key, message) {
		frappe.msgprint({
			title: options.title,
			message: `<p>${frappe.utils.escape_html(message || __("The invoice could not be uploaded."))}</p><p class="text-muted">${frappe.utils.escape_html(
				__("Nothing else is blocked. Retry sends the same upload again.")
			)}</p>`,
			indicator: "red",
			primary_action: {
				label: __("Retry"),
				action: () => {
					if (frappe.hide_msgprint) frappe.hide_msgprint();
					send(options, values, key);
				},
			},
		});
	}

	// One press (key empty) or one Retry (the key of the press): call the server, then wait for the job.
	function send(options, values, key) {
		const escape = frappe.utils.escape_html;
		let dialog = null;
		let timer = null;
		let settled = false;
		let sent = { values, key };
		const finish = (state, message, keep_waiting) => {
			if (settled) return;
			settled = true;
			if (timer) clearTimeout(timer);
			if (dialog) dialog.hide();
			if (keep_waiting) {
				frappe.show_alert({
					message: escape(__("Still sending to Noon. Refresh this document later to see the result.")),
					indicator: "blue",
				});
			} else if (state === "done") {
				frappe.show_alert({ message: escape(__("The invoice was uploaded to Noon.")), indicator: "green" });
				options.reload();
			} else {
				show_problem(options, sent.values, sent.key, message);
			}
		};
		dialog = new frappe.ui.Dialog({
			title: __("Sending to Noon..."),
			fields: [{ fieldtype: "HTML", fieldname: "body" }],
			primary_action_label: __("Close, keep sending"),
			primary_action: () => finish("pending", "", true),
			// closing the dialog ends the wait only: the job keeps running on the server
			on_hide: () => finish("pending", "", true),
		});
		dialog.show();
		dialog.fields_dict.body.$wrapper.html(`<p>${escape(__("Please wait. The invoice is being sent to Noon."))}</p>`);

		const started = Date.now();
		const poll = (idempotency_key) =>
			frappe
				.call({
					method: API + "check_invoice_upload",
					type: "POST",
					args: { document_type: options.doctype, document_name: options.name, idempotency_key },
					silent: true,
				})
				.then(
					(r) => {
						const result = (r && r.message) || {};
						if (result.state && result.state !== "pending") return finish(result.state, result.message);
						if (Date.now() - started >= timing.max_wait_ms) return finish("pending", "", true);
						timer = setTimeout(() => poll(idempotency_key), timing.poll_ms);
					},
					() => finish("pending", "", true)
				);
		return options.send(values, key).then(
			(r) => {
				const result = (r && r.message) || {};
				if (!result.queued) {
					settled = true;
					if (dialog) dialog.hide();
					frappe.show_alert({
						message: escape(__("A Noon fulfillment action for this order is already queued or running.")),
						indicator: "blue",
					});
					return;
				}
				// a Retry sends the attached copy of the PDF (the dialog's loose upload is gone)
				sent = {
					values: Object.assign({}, values, result.file_url ? { invoice_file: result.file_url } : {}),
					key: result.idempotency_key,
				};
				return poll(result.idempotency_key);
			},
			(error) => {
				if (settled) return;
				settled = true;
				if (dialog) dialog.hide();
				frappe.msgprint({
					title: options.title,
					message: escape(server_message(error) || __("The request could not be sent.")),
					indicator: "red",
				});
			}
		);
	}

	const exported = { start, show_form, send, show_problem, server_message, timing };

	if (typeof frappe !== "undefined") {
		frappe.marketplace_integrations = frappe.marketplace_integrations || {};
		frappe.marketplace_integrations.noon_invoice = exported;
	}

	if (typeof module !== "undefined") {
		module.exports = exported;
	}
})();
