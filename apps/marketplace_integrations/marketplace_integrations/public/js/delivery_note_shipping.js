// Copyright (c) 2026, Tamwenat El Zekrayat and contributors
// For license information, please see license.txt

// Carrier-neutral Shipping section of the Delivery Note form (epic #413, #408/#409).
//
// This shell owns what every carrier shares: the intro text per Integration Status, the "Sending to
// <carrier>..." dialog that Submit waits on, the Check / Refresh buttons and the shipping status
// indicator. A carrier plugs in by calling register_carrier() from its own file (see
// delivery_note_bosta.js) with the server methods and an optional add_buttons(frm). Which carriers
// show the section is decided by the server (frappe.boot.marketplace_shipping_carriers), and every
// rule is enforced server side (services/carrier_shipments.py): this script only shows and drives.
//
// Wrapped in a function: doctype_js files share one scope with ERPNext's own Delivery Note script.
(function () {
	const POLL_MS = 2000;
	const MAX_WAIT_MS = 60000;

	const NOT_CREATED = "Not Created";
	const CREATING = "Creating";
	const UNKNOWN = "Creation Unknown";
	const CREATED = "Created";
	const FAILED = "Creation Failed";
	const CANCEL_STATUSES = ["Cancellation Pending", "Cancellation Unknown", "Cancelled"];
	const EDITABLE = ["", NOT_CREATED, FAILED];

	const carriers = {};

	// carrier = { request_method, check_method, refresh_method, add_buttons(frm), pick_city(frm) } (all
	// optional but request_method; pick_city opens the carrier's city picker when the server answers
	// needs_city). The key is the Supplier name stored in Delivery Note.transporter_name.
	function register_carrier(name, carrier) {
		carriers[name] = carrier;
	}

	function carrier_name(frm) {
		const name = frm.doc && frm.doc.transporter_name;
		const enabled = (frappe.boot && frappe.boot.marketplace_shipping_carriers) || [];
		return name && enabled.includes(name) && carriers[name] ? name : null;
	}

	function status_of(frm) {
		return (frm.doc && frm.doc.custom_integration_status) || NOT_CREATED;
	}

	function is_editable(frm) {
		return Boolean(frm.doc && frm.doc.docstatus === 0 && EDITABLE.includes(frm.doc.custom_integration_status || ""));
	}

	// The message of a rejected frappe.call(): `_server_messages` is a JSON array of JSON strings (see
	// docs/frappe-conventions.md). The text is server HTML: it is parsed in an inert document and the
	// plain text is escaped again where it is shown.
	function server_message(error) {
		try {
			const messages = JSON.parse(error.responseJSON._server_messages || "[]");
			const texts = messages
				.map((entry) => new DOMParser().parseFromString(JSON.parse(entry).message || "", "text/html").body.textContent.trim())
				.filter(Boolean);
			if (texts.length) return texts.join(" ");
		} catch (e) {
			// fall through to the generic text
		}
		return "";
	}

	function show_intro(frm, carrier) {
		const status = status_of(frm);
		if (frm.is_new() || status === NOT_CREATED) return;
		const escape = frappe.utils.escape_html;
		const name = escape(carrier);
		// custom_last_integration_error is stored raw (it can hold carrier text): escape where shown.
		const error = escape(frm.doc.custom_last_integration_error || "");
		if (status === CREATING) {
			frm.set_intro(__("Sending the shipment to {0}...", [name]), "blue");
		} else if (status === CREATED) {
			frm.set_intro(
				__("{0} shipment created. Tracking number: {1}", [name, escape(frm.doc.lr_no || "")]),
				"green"
			);
		} else if (status === UNKNOWN) {
			frm.set_intro(
				__("It is not known whether {0} created the shipment. Use Check with {0} before trying again.", [name]) +
					(error ? `<br>${error}` : ""),
				"orange"
			);
		} else if (status === FAILED) {
			frm.set_intro(__("{0} did not create the shipment: {1}", [name, error]), "red");
		} else if (status === "Cancelled" && frm.doc.docstatus === 1) {
			// cancelled at the carrier, not yet in ERPNext: the next Cancel makes no carrier call
			frm.set_intro(
				__("Cancelled at {0}. This Delivery Note is still submitted: press Cancel to cancel it here. {0} will not be asked again.", [name]),
				"orange"
			);
		} else if (CANCEL_STATUSES.includes(status)) {
			frm.set_intro(__("{0} shipment: {1}", [name, escape(__(status))]), "yellow");
		}
	}

	function show_shipping_status(frm) {
		const status = frm.doc.custom_shipping_status;
		if (!status || frm.is_new() || !frm.dashboard || !frm.dashboard.add_indicator) return;
		// text carries the state, the colour is only extra
		frm.dashboard.add_indicator(
			__("Shipping status: {0}", [frappe.utils.escape_html(__(status))]),
			"blue"
		);
	}

	function call_and_reload(frm, method, freeze_message, on_result) {
		return frappe
			.call({ method, type: "POST", args: { delivery_note: frm.doc.name }, freeze: true, freeze_message })
			.then((r) => {
				if (on_result) on_result(r.message || {});
				return frm.reload_doc();
			});
	}

	// Asks the carrier whether it has the shipment (never creates), then reloads the form. The carrier's
	// text is escaped: frappe.show_alert renders HTML.
	function check_with_carrier(frm, carrier) {
		const escape = frappe.utils.escape_html;
		return call_and_reload(frm, carriers[carrier].check_method, __("Checking with {0}...", [carrier]), (result) => {
			frappe.show_alert({
				message: escape(
					result.status === CREATED
						? __("{0} has the shipment: it is now Created.", [carrier])
						: result.message || __("Still not known. Try again in a few minutes.")
				),
				indicator: result.status === CREATED ? "green" : "orange",
			});
		});
	}

	function add_buttons(frm, carrier) {
		if (frm.is_new() || !frm.doc || !frappe.model.can_write("Delivery Note")) return;
		const hooks = carriers[carrier];
		const status = status_of(frm);
		if (frm.doc.docstatus === 0 && [UNKNOWN, CREATING].includes(status) && hooks.check_method) {
			frm.add_custom_button(__("Check with {0}", [carrier]), () => check_with_carrier(frm, carrier));
		}
		if ([CREATED, ...CANCEL_STATUSES].includes(status) && hooks.refresh_method) {
			frm.add_custom_button(__("Refresh from {0}", [carrier]), () =>
				call_and_reload(frm, hooks.refresh_method, __("Reading the status from {0}...", [carrier]))
			);
		}
		if (hooks.add_buttons) hooks.add_buttons(frm);
	}

	// ---- Submit: wait for the carrier shipment, then let the native submit continue -------------

	function failure_html(carrier_name_, status, error) {
		const escape = frappe.utils.escape_html;
		const carrier = escape(carrier_name_);
		if (status === UNKNOWN) {
			return (
				`<p>${__("It is not known whether {0} created the shipment. The Delivery Note was not submitted.", [carrier])}</p>` +
				`<p class="text-muted">${__("Use Check with {0} on the Delivery Note before trying again.", [carrier])}</p>`
			);
		}
		if (status === FAILED) {
			// the error is stored raw (it can hold carrier text): escaped here
			return `<p>${__("{0} did not create the shipment: {1}", [carrier, escape(error || "")])}</p><p class="text-muted">${__("The Delivery Note was not submitted.")}</p>`;
		}
		return `<p>${__("{0} is taking longer than expected. The Delivery Note was not submitted.", [carrier])}</p><p class="text-muted">${__("Open the Delivery Note later: if the shipment is Created, submit it again.")}</p>`;
	}

	// Resolves once the shipment is Created (form reloaded, native submit continues); otherwise sets
	// frappe.validated = false and shows why.
	function before_submit(frm) {
		const carrier = carrier_name(frm);
		if (!carrier) return;
		if (status_of(frm) === CREATED) return;
		const hooks = carriers[carrier];
		if (frm.is_dirty()) {
			frappe.msgprint({ message: __("Save the Delivery Note first, then submit it."), indicator: "orange" });
			frappe.validated = false;
			return;
		}
		if (!frm.doc.custom_cod_confirmed) {
			frappe.msgprint({
				message: __("Confirm the COD amount before submitting: tick COD confirmed, or change the COD. 0 is allowed."),
				indicator: "orange",
			});
			frappe.validated = false;
			return;
		}
		return new Promise((resolve) => {
			let dialog = null;
			let timer = null;
			let settled = false;
			let keep_sending = false;
			const finish = (ok, status, error) => {
				if (settled) return;
				settled = true;
				if (timer) clearTimeout(timer);
				if (dialog) dialog.hide();
				if (ok) {
					// the worker changed the document: load it before the native submit saves it
					Promise.resolve(frm.reload_doc()).then(() => resolve());
					return;
				}
				frappe.validated = false;
				if (keep_sending) {
					frappe.show_alert({
						message: frappe.utils.escape_html(
							__("Still sending to {0}. Open the Delivery Note later: if the shipment is Created, submit it again.", [carrier])
						),
						indicator: "blue",
					});
				} else {
					frappe.msgprint({
						message: failure_html(carrier, status, error),
						indicator: "red",
						title: __("{0} shipment", [carrier]),
						primary_action:
							status === UNKNOWN && hooks.check_method
								? {
										label: __("Check with {0}", [carrier]),
										action: () => {
											if (frappe.hide_msgprint) frappe.hide_msgprint();
											return check_with_carrier(frm, carrier);
										},
									}
								: undefined,
					});
				}
				resolve();
			};
			dialog = new frappe.ui.Dialog({
				title: __("Sending to {0}...", [carrier]),
				fields: [{ fieldtype: "HTML", fieldname: "body" }],
				primary_action_label: __("Close, keep sending"),
				// The job keeps running on the server; only the wait ends.
				primary_action: () => {
					keep_sending = true;
					finish(false, CREATING);
				},
				// Closing the dialog abandons the wait only: the job keeps running on the server.
				on_hide: () => finish(false, CREATING),
			});
			dialog.show();
			dialog.fields_dict.body.$wrapper.html(
				`<p>${frappe.utils.escape_html(__("Please wait. The Delivery Note is submitted once {0} has created the shipment.", [carrier]))}</p>`
			);
			const started = Date.now();
			const poll = () =>
				frappe.db
					.get_value("Delivery Note", frm.doc.name, ["custom_integration_status", "custom_last_integration_error"])
					.then(
						(r) => {
							const row = (r && r.message) || {};
							const status = row.custom_integration_status || NOT_CREATED;
							if (status === CREATED) return finish(true);
							if (status === FAILED || status === UNKNOWN) return finish(false, status, row.custom_last_integration_error);
							if (Date.now() - started >= api.max_wait_ms) return finish(false, CREATING);
							timer = setTimeout(poll, api.poll_ms);
						},
						() => finish(false, CREATING)
					);
			frappe
				.call({
					method: hooks.request_method,
					type: "POST",
					args: { delivery_note: frm.doc.name, modified: frm.doc.modified },
					// the server's refusal is shown below (parsed and escaped), not by frappe.call
					silent: true,
				})
				.then(
					(response) => {
						const result = (response && response.message) || {};
						if (result.needs_city && hooks.pick_city) {
							// the server found no city for the address: ask the person, who then saves and submits again
							if (settled) return;
							settled = true;
							if (dialog) dialog.hide();
							frappe.validated = false;
							frappe.show_alert({ message: __("Choose the {0} city, then submit again.", [carrier]), indicator: "orange" });
							hooks.pick_city(frm);
							resolve();
							return;
						}
						return poll();
					},
					(error) => {
						// the server refused (validation)
						if (settled) return;
						settled = true;
						if (dialog) dialog.hide();
						frappe.validated = false;
						frappe.msgprint({
							message: frappe.utils.escape_html(server_message(error) || __("The shipment could not be sent.")),
							indicator: "red",
							title: __("{0} shipment", [carrier]),
						});
						resolve();
					}
				);
		});
	}

	function money(value, currency) {
		return typeof format_currency === "function" ? format_currency(value, currency) : String(value);
	}

	// A confirmed COD is never recomputed (it may be a deliberate choice), so when the items or the
	// advance changed since, say what the computed amount is. The merchant decides; nothing is written.
	// The saved note is judged by the server, so an unsaved form shows no hint.
	function show_cod_hint(frm, carrier) {
		const hooks = carriers[carrier];
		if (!hooks.cod_hint_method || frm.is_new() || frm.is_dirty() || !is_editable(frm) || !frm.doc.custom_cod_confirmed) return;
		if (!frm.dashboard || !frm.dashboard.add_comment) return;
		return frappe
			.call({ method: hooks.cod_hint_method, type: "POST", args: { delivery_note: frm.doc.name } })
			.then((r) => {
				const hint = r && r.message;
				if (!hint || hint.default === null || hint.default === undefined) return;
				frm.dashboard.add_comment(
					frappe.utils.escape_html(
						__("The COD amount {0} differs from the amount calculated from the current items and advance ({1}). Change the COD if it is no longer right.", [
							money(frm.doc.custom_cod_amount, frm.doc.currency),
							money(hint.default, frm.doc.currency),
						])
					),
					"yellow",
					true
				);
			})
			.catch(() => {
				// a hint only: a failed lookup must not disturb the form
			});
	}

	// The COD amount and its confirmation can only change while the note is editable.
	function set_cod_state(frm) {
		const read_only = is_editable(frm) ? 0 : 1;
		frm.set_df_property("custom_cod_amount", "read_only", read_only);
		frm.set_df_property("custom_cod_confirmed", "read_only", read_only);
	}

	const api = {
		register_carrier,
		poll_ms: POLL_MS,
		max_wait_ms: MAX_WAIT_MS,
		carrier_name,
		failure_html,
		server_message,
		is_editable,
		show_cod_hint,
	};

	if (typeof frappe !== "undefined") {
		frappe.marketplace_integrations = frappe.marketplace_integrations || {};
		frappe.marketplace_integrations.shipping = api;
		if (frappe.ui && frappe.ui.form) {
			frappe.ui.form.on("Delivery Note", {
				refresh(frm) {
					const carrier = carrier_name(frm);
					if (!carrier) return;
					set_cod_state(frm);
					show_intro(frm, carrier);
					show_shipping_status(frm);
					show_cod_hint(frm, carrier);
					add_buttons(frm, carrier);
				},
				// Editing the COD is the person's choice: it confirms the amount.
				custom_cod_amount(frm) {
					if (carrier_name(frm) && is_editable(frm) && !frm.doc.custom_cod_confirmed) {
						frm.set_value("custom_cod_confirmed", 1);
					}
				},
				before_submit,
			});
		}
	}

	if (typeof module !== "undefined") {
		module.exports = api;
	}
})();
