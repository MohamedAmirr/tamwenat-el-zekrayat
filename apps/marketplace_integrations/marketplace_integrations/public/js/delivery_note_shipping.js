// Copyright (c) 2026, Tamwenat El Zekrayat and contributors
// For license information, please see license.txt

// Carrier-neutral Shipping section of the Delivery Note form (epic #413, #408/#409).
//
// This shell owns what every carrier shares: the intro text per Integration Status, the "Sending to
// <carrier>..." dialog that Submit waits on, the Check / Refresh buttons and the shipping status
// indicator. A carrier plugs in by calling register_carrier() from its own file (see
// the carrier's own delivery_note_<carrier>.js file) with optional before_send(frm) -> bool, refresh(frm), add_buttons(frm) and
// pick_city(frm). The shell calls the neutral API (api/carrier_shipments.py). Which carriers show the
// section is decided by the server (frappe.boot.marketplace_shipping_carriers), and every rule is
// enforced server side (services/carrier_shipments.py): this script only shows and drives.
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
	const RETURN_PICKUP = "Customer Return Pickup";
	// Not a stored status: the wait ended because the shipment is Created but the job could not submit the note.
	const SUBMIT_FAILED = "Submit Failed";

	const API = "marketplace_integrations.api.carrier_shipments.";
	const carriers = {};

	// carrier = { before_send(frm) -> bool, refresh(frm), add_buttons(frm), pick_city(frm), locked_fields }
	// (all optional). before_send returns false to stop the Submit (it shows why); refresh runs on every
	// form refresh; pick_city opens the carrier's picker when the server answers needs_city; locked_fields
	// are read-only once the shipment is started. The key is the Supplier name stored in
	// Delivery Note.transporter_name.
	function register_carrier(name, carrier) {
		carriers[name] = carrier;
	}

	// A return is a carrier note only when the staff chose the carrier to collect it.
	function carrier_name(frm) {
		if (frm.doc && frm.doc.is_return && frm.doc.custom_return_source !== RETURN_PICKUP) return null;
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

	// One carrier request per Delivery Note at a time: a second click (or another carrier button) while one
	// is in flight does nothing. The lock is held until the request and the form reload are over, success or
	// failure. `on_busy` runs instead of `fn` when the note is busy (a flow that waits to be told how it ended).
	const busy = new Set();
	function is_busy(frm) {
		return busy.has(frm.doc.name);
	}
	function exclusive(frm, fn, on_busy) {
		if (is_busy(frm)) {
			// the click did nothing: say why
			frappe.show_alert({ message: __("A shipping request is still running."), indicator: "orange" });
			return Promise.resolve(on_busy ? on_busy() : undefined);
		}
		const key = frm.doc.name;
		busy.add(key);
		let result;
		try {
			result = Promise.resolve(fn());
		} catch (error) {
			busy.delete(key);
			return Promise.reject(error);
		}
		return result.finally(() => busy.delete(key));
	}

	function call_and_reload(frm, method, freeze_message, on_result) {
		return exclusive(frm, () =>
			frappe
				.call({ method, type: "POST", args: { delivery_note: frm.doc.name }, freeze: true, freeze_message })
				.then((r) => {
					if (on_result) on_result(r.message || {});
					return frm.reload_doc();
				})
		);
	}

	// Asks the carrier whether it has the shipment (never creates), then reloads the form. The carrier's
	// text is escaped: frappe.show_alert renders HTML.
	function check_with_carrier(frm, carrier) {
		const escape = frappe.utils.escape_html;
		return call_and_reload(frm, API + "check_shipment", __("Checking with {0}...", [carrier]), (result) => {
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
		if (frm.doc.docstatus === 0 && [UNKNOWN, CREATING].includes(status)) {
			frm.add_custom_button(__("Check with {0}", [carrier]), () => check_with_carrier(frm, carrier));
		}
		if ([CREATED, ...CANCEL_STATUSES].includes(status)) {
			frm.add_custom_button(__("Refresh from {0}", [carrier]), () =>
				call_and_reload(frm, API + "refresh_shipment", __("Reading the status from {0}...", [carrier]), (result) => {
					// Already Cancelled here and the carrier was asked and still shows it cancelled: nothing changed,
					// say so. Only "Terminated" means that: Noon's refresh of a Cancelled note returns the stored
					// status ("Cancelled") without asking Noon.
					if (status === "Cancelled" && result.shipping_status === "Terminated") {
						frappe.show_alert({
							message: frappe.utils.escape_html(
								__("The carrier shows this shipment as cancelled. Nothing else to refresh.")
							),
							indicator: "green",
						});
					}
				})
			);
		}
		if (hooks.add_buttons) hooks.add_buttons(frm);
	}

	// ---- Submit: wait for the carrier shipment, then let the native submit continue -------------

	// `submitting` is false for a button request (the return pickup): nothing was being submitted then.
	function failure_html(carrier_name_, status, error, submitting = true) {
		const escape = frappe.utils.escape_html;
		const carrier = escape(carrier_name_);
		if (!submitting) {
			if (status === UNKNOWN) {
				return (
					`<p>${__("It is not known whether {0} created the request.", [carrier])}</p>` +
					`<p class="text-muted">${__("Use Check with {0} on the Delivery Note before trying again.", [carrier])}</p>`
				);
			}
			if (status === FAILED) {
				return `<p>${__("{0} did not create the request: {1}", [carrier, escape(error || "")])}</p>`;
			}
			return `<p>${__("{0} is taking longer than expected.", [carrier])}</p><p class="text-muted">${__("Open the Delivery Note later and check its Integration Status.")}</p>`;
		}
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
		if (status === SUBMIT_FAILED) {
			// the shipment exists; the job's own submit failed. The stored text names the reason (raw: escaped here).
			return `<p>${escape(error || "")}</p><p class="text-muted">${__("The shipment exists at {0}. Fix the problem, then submit the Delivery Note again.", [carrier])}</p>`;
		}
		return `<p>${__("{0} is taking longer than expected.", [carrier])}</p><p class="text-muted">${__("The Delivery Note is submitted automatically once the shipment is created. Open the Delivery Note later: if the shipment is Created and it is still a draft, submit it again.")}</p>`;
	}

	// The server (the background job) submits the note once the carrier confirms Created, so this only waits
	// and reloads. It always sets frappe.validated = false so Frappe's own save never runs a second time on a
	// stale copy; when the shipment was not created it shows why.
	function before_submit(frm) {
		const carrier = carrier_name(frm);
		// A return is submitted without waiting: submitting it is the receipt; its pickup is its own button.
		if (!carrier || frm.doc.is_return) return;
		if (status_of(frm) === CREATED) return;
		const hooks = carriers[carrier];
		if (frm.is_dirty()) {
			frappe.msgprint({ message: __("Save the Delivery Note first, then submit it."), indicator: "orange" });
			frappe.validated = false;
			return;
		}
		if (hooks.before_send && hooks.before_send(frm) === false) {
			frappe.validated = false;
			return;
		}
		return send_and_wait(frm, carrier, true);
	}

	// Starts the carrier request (the create of a delivery, or the pickup of a return) and waits for it in a
	// dialog. Resolves once the note is Created (form reloaded); otherwise sets frappe.validated = false and
	// shows why. `submitting` is true when it is the native Submit that waits.
	function send_and_wait(frm, carrier, submitting) {
		// a request already in flight: this click starts nothing and the submit must not go on
		return exclusive(frm, () => send_and_wait_locked(frm, carrier, submitting), () => {
			frappe.validated = false;
		});
	}

	function send_and_wait_locked(frm, carrier, submitting) {
		const hooks = carriers[carrier];
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
				// The job submitted the note (or the person only requested a pickup): Frappe's own save must not
				// run a second time on this stale copy.
				frappe.validated = false;
				if (ok) {
					Promise.resolve(frm.reload_doc()).then(() => resolve());
					return;
				}
				if (status === SUBMIT_FAILED) {
					frappe.msgprint({
						message: failure_html(carrier, status, error, submitting),
						indicator: "orange",
						title: __("{0} shipment", [carrier]),
					});
					Promise.resolve(frm.reload_doc()).then(() => resolve());
					return;
				}
				if (keep_sending) {
					frappe.show_alert({
						message: frappe.utils.escape_html(
							submitting
								? __("Still sending to {0}. The Delivery Note is submitted automatically once the shipment is created.", [carrier])
								: __("Still sending to {0}. Open the Delivery Note later and check its Integration Status.", [carrier])
						),
						indicator: "blue",
					});
				} else {
					frappe.msgprint({
						message: failure_html(carrier, status, error, submitting),
						indicator: "red",
						title: __("{0} shipment", [carrier]),
						primary_action:
							status === UNKNOWN
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
				`<p>${frappe.utils.escape_html(
						submitting
							? __("Please wait. The Delivery Note is submitted automatically once {0} has created the shipment.", [carrier])
							: __("Please wait. Sending the return pickup request to {0}.", [carrier])
					)}</p>`
			);
			const started = Date.now();
			const poll = () =>
				frappe.db
					.get_value("Delivery Note", frm.doc.name, ["docstatus", "custom_integration_status", "custom_last_integration_error"])
					.then(
						(r) => {
							if (settled) return;
							const row = (r && r.message) || {};
							const status = row.custom_integration_status || NOT_CREATED;
							if (status === CREATED) {
								// a pickup request ends at Created; a delivery ends when the job has submitted the note
								if (!submitting || row.docstatus === 1) return finish(true);
								// Created but still a draft with an error: the job's submit failed
								if (row.custom_last_integration_error) {
									return finish(false, SUBMIT_FAILED, row.custom_last_integration_error);
								}
							}
							if (status === FAILED || status === UNKNOWN) return finish(false, status, row.custom_last_integration_error);
							if (Date.now() - started >= api.max_wait_ms) return finish(false, CREATING);
							timer = setTimeout(poll, api.poll_ms);
						},
						() => finish(false, CREATING)
					);
			frappe
				.call({
					method: API + "request_shipment",
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
							frappe.show_alert({
								message: submitting
									? __("Choose the {0} city, then submit again.", [carrier])
									: __("Choose the {0} city, save, then request the pickup again.", [carrier]),
								indicator: "orange",
							});
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

	// Fields the carrier was sent (carrier.locked_fields) lock once the shipment is started; the server
	// refuses a change too.
	function set_locked_fields(frm, carrier) {
		const locked = !is_editable(frm);
		((carriers[carrier] && carriers[carrier].locked_fields) || []).forEach((fieldname) => {
			// unlocking restores the field's own setting, not 0 (a field read-only by design stays so)
			const df = frappe.meta && frappe.meta.get_docfield ? frappe.meta.get_docfield("Delivery Note", fieldname) : null;
			frm.set_df_property(fieldname, "read_only", locked ? 1 : (df && df.read_only) || 0);
		});
	}

	const api = {
		register_carrier,
		poll_ms: POLL_MS,
		max_wait_ms: MAX_WAIT_MS,
		carrier_name,
		failure_html,
		send_and_wait,
		exclusive,
		is_busy,
		server_message,
		is_editable,
	};

	if (typeof frappe !== "undefined") {
		frappe.marketplace_integrations = frappe.marketplace_integrations || {};
		frappe.marketplace_integrations.shipping = api;
		if (frappe.ui && frappe.ui.form) {
			frappe.ui.form.on("Delivery Note", {
				refresh(frm) {
					const carrier = carrier_name(frm);
					if (!carrier) return;
					set_locked_fields(frm, carrier);
					show_intro(frm, carrier);
					show_shipping_status(frm);
					if (carriers[carrier].refresh) carriers[carrier].refresh(frm);
					add_buttons(frm, carrier);
				},
				before_submit,
			});
		}
	}

	if (typeof module !== "undefined") {
		module.exports = api;
	}
})();
