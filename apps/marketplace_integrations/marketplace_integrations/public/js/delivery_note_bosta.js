// Copyright (c) 2026, Tamwenat El Zekrayat and contributors
// For license information, please see license.txt

// Bosta part of the Delivery Note Shipping section (epic #413). delivery_note_shipping.js owns the
// shared behaviour; this file registers Bosta with it and adds the Bosta-only pieces: the COD rules
// (confirm before Submit, the differs hint), choosing the Bosta city (Bosta is sent a city code), fetching
// the A4 Arabic label, cancelling the
// shipment at Bosta before the native cancel (and the "Cancel Bosta Shipment" button of a draft note, whose
// delete the server refuses until then) and editing the receiver phone and address (#410).
//
// Wrapped in a function: doctype_js files share one scope with ERPNext's own Delivery Note script.
(function () {
	const API = "marketplace_integrations.api.bosta_shipments.";
	// request, check, refresh and cancel are neutral
	const CARRIER_API = "marketplace_integrations.api.carrier_shipments.";
	const EDITABLE = ["", "Not Created", "Creation Failed"];
	// Integration Statuses with a Bosta shipment that must be cancelled at Bosta before ERPNext cancels.
	const CANCELLABLE = ["Created", "Cancellation Pending", "Cancellation Unknown"];
	const RETURN_PICKUP = "Customer Return Pickup";
	const ADDRESS_LINE_MIN = 6;
	const ADDRESS_LINE_MAX = 240;

	function city_label(city) {
		return city.name_ar && city.name_ar !== city.name ? `${city.name} - ${city.name_ar}` : city.name;
	}

	// Opens the city dialog; the chosen city's code and name are written to the (unsaved) form.
	function pick_city(frm) {
		return frappe.call({ method: API + "get_cities", type: "POST", args: { delivery_note: frm.doc.name }, freeze: true }).then((r) => {
			const data = r.message || {};
			const cities = Array.isArray(data.cities) ? data.cities : [];
			if (!cities.length) {
				frappe.msgprint({ message: __("Bosta's cities could not be loaded. Try again in a minute."), indicator: "orange" });
				return;
			}
			const suggested = (data.suggested && data.suggested.city_id) || "";
			const dialog = new frappe.ui.Dialog({
				title: __("Pick Bosta City"),
				fields: [
					{
						fieldtype: "Autocomplete",
						fieldname: "city",
						label: __("Bosta City"),
						reqd: 1,
						options: cities.map((city) => ({ label: city_label(city), value: city.id })),
						default: suggested,
						description: suggested ? __("Suggested from the delivery address.") : "",
					},
				],
				primary_action_label: __("Use this city"),
				primary_action: (values) => {
					dialog.hide();
					return frappe
						.call({ method: API + "resolve_city", type: "POST", args: { city_id: values.city }, freeze: true })
						.then((res) => {
							const city = res.message || {};
							if (!city.code) return;
							return frm
								.set_value({ custom_bosta_city: city.code, custom_bosta_city_name: city.name })
								.then(() => frappe.show_alert({ message: __("City set. Save the Delivery Note."), indicator: "green" }));
						});
				},
			});
			dialog.show();
		});
	}

	function fetch_label(frm) {
		return frappe
			.call({ method: API + "fetch_label", type: "POST", args: { delivery_note: frm.doc.name }, freeze: true, freeze_message: __("Fetching the label...") })
			.then(() => frm.reload_doc());
	}


	function escape(text) {
		return frappe.utils.escape_html(text || "");
	}

	// The message of a rejected frappe.call(), plain text (the shell parses the server HTML).
	function message_of(error) {
		const shipping = frappe.marketplace_integrations && frappe.marketplace_integrations.shipping;
		return (shipping && shipping.server_message && shipping.server_message(error)) || "";
	}

	// ---- COD ------------------------------------------------------------------------------------

	function is_editable(frm) {
		return Boolean(frm.doc && frm.doc.docstatus === 0 && EDITABLE.includes(frm.doc.custom_integration_status || ""));
	}

	function money(value, currency) {
		return typeof format_currency === "function" ? format_currency(value, currency) : String(value);
	}

	// Submit gate: the COD amount must be confirmed. Returns false (after saying why) to stop the Submit.
	function before_send(frm) {
		if (frm.doc.custom_cod_confirmed) return true;
		frappe.msgprint({
			message: __("Confirm the COD amount before submitting: tick COD confirmed, or change the COD. 0 is allowed."),
			indicator: "orange",
		});
		return false;
	}

	// A confirmed COD is never recomputed (it may be a deliberate choice), so when the items or the
	// advance changed since, say what the computed amount is. The merchant decides; nothing is written.
	// The saved note is judged by the server, so an unsaved form shows no hint.
	function show_cod_hint(frm) {
		if (frm.is_new() || frm.is_dirty() || !is_editable(frm) || !frm.doc.custom_cod_confirmed) return;
		if (!frm.dashboard || !frm.dashboard.add_comment) return;
		return frappe
			.call({ method: API + "get_cod_hint", type: "POST", args: { delivery_note: frm.doc.name } })
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

	function refresh(frm) {
		set_cod_state(frm);
		show_cod_hint(frm);
	}

	// Editing the COD is the person's choice: it confirms the amount.
	function custom_cod_amount(frm) {
		if (frm.doc.transporter_name === "Bosta" && is_editable(frm) && !frm.doc.custom_cod_confirmed) {
			frm.set_value("custom_cod_confirmed", 1);
		}
	}

	// ---- Cancel at Bosta ------------------------------------------------------------------------

	// Calls the server once. `done` is called when Bosta confirmed (or the note was already Cancelled);
	// `stop` when the cancel must not go on (a refusal, an unknown result or an error: a dialog says why).
	function cancel_at_bosta(frm, { done, stop }) {
		const retry = () => cancel_at_bosta(frm, { done, stop });
		return frappe
			.call({
				method: CARRIER_API + "cancel_shipment",
				type: "POST",
				args: { delivery_note: frm.doc.name },
				freeze: true,
				freeze_message: __("Cancelling at Bosta..."),
				// the server's refusal is shown below (parsed and escaped), not by frappe.call
				silent: true,
			})
			.then(
				(r) => {
					const result = r.message || {};
					if (result.outcome === "cancelled") return done(result);
					if (result.outcome === "refused") return show_refused(result.message, stop);
					return show_unknown(frm, result.message, { done, stop, retry });
				},
				(error) => {
					frappe.msgprint({
						message: escape(message_of(error) || __("The shipment could not be cancelled at Bosta.")),
						indicator: "red",
						title: __("Bosta shipment"),
					});
					return stop();
				}
			);
	}

	function show_refused(message, stop) {
		frappe.msgprint({
			message: `<p>${__("Bosta refused: {0}", [escape(message)])}</p><p class="text-muted">${escape(
				__("Nothing was cancelled in ERPNext.")
			)}</p>`,
			indicator: "red",
			title: __("Bosta shipment"),
		});
		return stop();
	}

	// Timeout, a temporary error or an answer that does not settle it: nothing was cancelled in ERPNext.
	// Retry asks Bosta again; Check with Bosta only reads the state; closing ends the attempt.
	function show_unknown(frm, message, { done, stop, retry }) {
		let acted = false;
		const check = () =>
			frappe
				.call({
					method: CARRIER_API + "refresh_shipment",
					type: "POST",
					args: { delivery_note: frm.doc.name },
					freeze: true,
					freeze_message: __("Checking with Bosta..."),
					silent: true,
				})
				.then(
					() => Promise.resolve(frm.reload_doc()).then(() => (frm.doc.custom_integration_status === "Cancelled" ? done({}) : show_unknown(frm, __("Bosta still shows the shipment as not cancelled."), { done, stop, retry }))),
					(error) => show_unknown(frm, message_of(error), { done, stop, retry })
				);
		const dialog = new frappe.ui.Dialog({
			title: __("Bosta shipment"),
			fields: [{ fieldtype: "HTML", fieldname: "body" }],
			primary_action_label: __("Retry"),
			primary_action: () => {
				acted = true;
				dialog.hide();
				return retry();
			},
			secondary_action_label: __("Check with Bosta"),
			secondary_action: () => {
				acted = true;
				dialog.hide();
				return check();
			},
			on_hide: () => {
				if (!acted) stop();
			},
		});
		dialog.show();
		dialog.fields_dict.body.$wrapper.html(
			`<p>${escape(__("Bosta did not confirm the cancellation. Nothing was cancelled in ERPNext."))}</p>` +
				(message ? `<p class="text-muted">${escape(message)}</p>` : "")
		);
		return dialog;
	}

	// Form hook: runs after the native "Permanently Cancel" confirmation. Resolves when the native cancel
	// may go on; sets frappe.validated = false (and shows why) when it must not.
	function before_cancel(frm) {
		if (frm.doc.transporter_name !== "Bosta" || !CANCELLABLE.includes(frm.doc.custom_integration_status || "")) return;
		return new Promise((resolve) => {
			cancel_at_bosta(frm, {
				// the server changed the note: load it before the native cancel saves it
				done: () => Promise.resolve(frm.reload_doc()).then(() => resolve()),
				stop: () => {
					frappe.validated = false;
					resolve();
				},
			});
		});
	}

	// A draft note with a live shipment cannot be deleted until the shipment is cancelled at Bosta.
	function cancel_shipment(frm) {
		return new Promise((resolve) => {
			frappe.confirm(
				__("Cancel the Bosta shipment of this Delivery Note? This cannot be undone."),
				() =>
					cancel_at_bosta(frm, {
						done: () =>
							Promise.resolve(frm.reload_doc()).then(() => {
								frappe.show_alert({ message: __("The Bosta shipment was cancelled. The Delivery Note can now be deleted."), indicator: "green" });
								resolve();
							}),
						stop: () => resolve(),
					}),
				() => resolve()
			);
		});
	}

	// ---- Edit receiver phone and address --------------------------------------------------------

	// An error text, or "" when the values may be sent.
	function edit_error(values) {
		if (!(values.phone || "").trim()) return __("Enter the receiver phone.");
		const length = (values.first_line || "").trim().length;
		if (length < ADDRESS_LINE_MIN) return __("Address First Line must be more than 5 characters.");
		if (length > ADDRESS_LINE_MAX) return __("Address First Line must be at most {0} characters.", [ADDRESS_LINE_MAX]);
		if (!values.city) return __("Choose the Bosta city.");
		return "";
	}

	function edit_shipment(frm) {
		const loaded = Promise.all([
			frappe.call({ method: API + "get_cities", type: "POST", args: { delivery_note: frm.doc.name }, freeze: true }),
			frappe.call({ method: API + "get_edit_values", type: "POST", args: { delivery_note: frm.doc.name } }),
		]);
		return loaded.then(([cities_response, values_response]) => {
			const cities = ((cities_response.message || {}).cities) || [];
			const current = values_response.message || {};
			if (!cities.length) {
				frappe.msgprint({ message: __("Bosta's cities could not be loaded. Try again in a minute."), indicator: "orange" });
				return;
			}
			const show_blur_error = (field) => {
				const values = dialog.get_values(true) || {};
				const error = edit_error(values);
				const wanted = field === "phone" ? !(values.phone || "").trim() : (values.first_line || "").trim().length < ADDRESS_LINE_MIN || (values.first_line || "").trim().length > ADDRESS_LINE_MAX;
				dialog.get_field(field).set_description(wanted ? error : "");
			};
			const dialog = new frappe.ui.Dialog({
				title: __("Edit Bosta Shipment"),
				fields: [
					{ fieldtype: "Data", fieldname: "phone", label: __("Receiver Phone"), reqd: 1, default: current.phone || "", change: () => show_blur_error("phone") },
					{
						fieldtype: "Autocomplete",
						fieldname: "city",
						label: __("Bosta City"),
						reqd: 1,
						options: cities.map((city) => ({ label: city_label(city), value: city.id })),
						default: current.city_id || "",
					},
					{ fieldtype: "Small Text", fieldname: "first_line", label: __("Address First Line"), reqd: 1, default: current.first_line || "", change: () => show_blur_error("first_line") },
				],
				primary_action_label: __("Send to Bosta"),
				primary_action: (values) => {
					const error = edit_error(values);
					if (error) {
						frappe.msgprint({ message: escape(error), indicator: "orange" });
						return;
					}
					// one call at a time: the button stays off until the answer is back
					dialog.get_primary_btn().prop("disabled", true);
					return frappe
						.call({
							method: API + "edit_shipment",
							type: "POST",
							args: { delivery_note: frm.doc.name, receiver_phone: values.phone, city_id: values.city, address_first_line: values.first_line },
							freeze: true,
							freeze_message: __("Sending to Bosta..."),
							silent: true,
						})
						.then(
							() => {
								dialog.hide();
								frappe.show_alert({ message: __("Bosta updated the delivery."), indicator: "green" });
								return frm.reload_doc();
							},
							(failure) => {
								dialog.get_primary_btn().prop("disabled", false);
								frappe.msgprint({
									message: escape(message_of(failure) || __("The delivery could not be updated at Bosta.")),
									indicator: "red",
									title: __("Bosta shipment"),
								});
							}
						);
				},
			});
			dialog.show();
			return dialog;
		});
	}

	// ---- Customer Return Pickup (type 25) ---------------------------------------------------------

	// The button shows on a draft return the staff marked "Customer Return Pickup", until a pickup is started.
	function can_request_pickup(frm) {
		return Boolean(
			frm.doc &&
				frm.doc.is_return &&
				frm.doc.custom_return_source === RETURN_PICKUP &&
				frm.doc.docstatus === 0 &&
				EDITABLE.includes(frm.doc.custom_integration_status || "")
		);
	}

	// Same wait dialog, Unknown / Check / Retry pattern as the outbound create (the neutral shell owns it).
	function request_pickup(frm) {
		if (frm.is_dirty()) {
			frappe.msgprint({ message: __("Save the Delivery Note first, then request the pickup."), indicator: "orange" });
			return Promise.resolve();
		}
		const shipping = frappe.marketplace_integrations.shipping;
		return shipping.send_and_wait(frm, "Bosta", false);
	}

	function add_buttons(frm) {
		const status = frm.doc.custom_integration_status || "";
		const is_return = Boolean(frm.doc.is_return);
		if (frm.doc.docstatus === 0 && EDITABLE.includes(status)) {
			frm.add_custom_button(__("Pick Bosta City"), () => pick_city(frm));
		}
		if (can_request_pickup(frm)) {
			frm.add_custom_button(__("Request Bosta Return Pickup"), () => request_pickup(frm));
		}
		if (status === "Created" && !frm.doc.custom_shipping_label && !is_return) {
			frm.add_custom_button(__("Fetch Bosta Label"), () => fetch_label(frm));
		}
		if (status === "Created" && !is_return) {
			frm.add_custom_button(__("Edit Bosta Shipment"), () => edit_shipment(frm));
		}
		if (frm.doc.docstatus === 0 && CANCELLABLE.includes(status)) {
			frm.add_custom_button(__("Cancel Bosta Shipment"), () => cancel_shipment(frm));
		}
	}

	const hooks = {
		before_send,
		refresh,
		pick_city,
		add_buttons,
		// Sent to Bosta; after the start only Edit Bosta Shipment changes them.
		locked_fields: ["contact_mobile", "shipping_address_name", "shipping_address"],
	};


	if (typeof frappe !== "undefined" && frappe.marketplace_integrations && frappe.marketplace_integrations.shipping) {
		frappe.marketplace_integrations.shipping.register_carrier("Bosta", hooks);
		if (frappe.ui && frappe.ui.form) {
			frappe.ui.form.on("Delivery Note", { before_cancel, custom_cod_amount });
		}
	}

	if (typeof module !== "undefined") {
		module.exports = {
			hooks,
			pick_city,
			fetch_label,
			add_buttons,
			can_request_pickup,
			request_pickup,
			city_label,
			before_cancel,
			custom_cod_amount,
			before_send,
			show_cod_hint,
			set_cod_state,
			cancel_shipment,
			edit_shipment,
			edit_error,
		};
	}
})();
