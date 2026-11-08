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

	// One carrier request per Delivery Note at a time (the shell holds the lock); runs `fn` directly without it.
	function exclusive(frm, fn, on_busy) {
		const shipping = frappe.marketplace_integrations && frappe.marketplace_integrations.shipping;
		return shipping && shipping.exclusive ? shipping.exclusive(frm, fn, on_busy) : Promise.resolve(fn());
	}

	function fetch_label(frm) {
		return exclusive(frm, () =>
			frappe
				.call({ method: API + "fetch_label", type: "POST", args: { delivery_note: frm.doc.name }, freeze: true, freeze_message: __("Fetching the label...") })
				.then(() => frm.reload_doc())
		);
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
			message: __("Confirm the COD amount before creating the shipment: tick COD confirmed, or change the COD. 0 is allowed."),
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

	// ---- Live COD prefill ------------------------------------------------------------------------
	// The server rule (default_cod) stays the single source of truth: while a note is being CREATED (new,
	// unsaved) the browser asks it for the amount of the form and shows it, still editable and NOT confirmed.
	// A saved note is never prefilled by the browser: the server fills or recomputes the COD on save (until the
	// person confirms it). A typed or confirmed amount is never overwritten.
	// Frappe reuses one `frm` for many documents, so the state (`__cod_*`) is reset when the document changes.

	const COD_FIELD = "custom_cod_amount";
	const PREFILL_DELAY_MS = 400;

	// The shell shows the shipping section for a carrier only when the server enabled it.
	function carrier_enabled() {
		const enabled = (frappe.boot && frappe.boot.marketplace_shipping_carriers) || [];
		return enabled.includes("Bosta");
	}

	// The child doctypes (taxes) are shared with other documents: only a Delivery Note form is handled.
	function is_creating(frm) {
		if (frm.doctype && frm.doctype !== "Delivery Note") return false;
		return Boolean(frm.is_new && frm.is_new());
	}

	function can_prefill(frm) {
		const doc = frm.doc || {};
		return (
			carrier_enabled() &&
			is_creating(frm) &&
			doc.transporter_name === "Bosta" &&
			!doc.is_return &&
			is_editable(frm) &&
			!doc.custom_cod_confirmed
		);
	}

	// What the answer depends on: a response for another carrier, item set or total is stale.
	function cod_key(frm) {
		const doc = frm.doc;
		return JSON.stringify([doc.transporter_name, doc.grand_total, (doc.items || []).map((row) => row.against_sales_order)]);
	}

	function cod_description(frm, text) {
		const field = frm.fields_dict && frm.fields_dict[COD_FIELD];
		if (field && field.df && frm.__cod_description === undefined) frm.__cod_description = field.df.description || "";
		frm.set_df_property(COD_FIELD, "description", text === null ? frm.__cod_description || "" : text);
	}

	// Drops the prefill marker and its text (never the amount).
	function forget_prefill(frm) {
		const had = frm.__cod_prefilled !== undefined;
		delete frm.__cod_prefilled;
		if (had) cod_description(frm, null);
	}

	// A form reused for another document starts clean: no timer, marker or text of the previous one.
	function reset_state(frm) {
		clearTimeout(frm.__cod_timer);
		frm.__cod_timer = undefined;
		forget_prefill(frm);
		frm.__cod_description = undefined;
		frm.__cod_programmatic = false;
		frm.__cod_doc = frm.doc && frm.doc.name;
	}

	function ensure_state(frm) {
		if (frm.__cod_doc !== (frm.doc && frm.doc.name)) reset_state(frm);
	}

	// The programmatic write: the flag keeps the amount handler from reading it as the person's edit.
	function set_cod(frm, value) {
		frm.__cod_programmatic = true;
		return Promise.resolve(frm.set_value(COD_FIELD, value)).finally(() => {
			frm.__cod_programmatic = false;
		});
	}

	function show_prefilled(frm, amount) {
		cod_description(frm, __("Filled from the order: {0}. You can change it.", [money(amount, frm.doc.currency)]));
	}

	// `retried`: a stale answer asks once more (never loops).
	function prefill_cod(frm, retried) {
		ensure_state(frm);
		if (!can_prefill(frm)) return Promise.resolve();
		const key = cod_key(frm);
		const name = frm.doc.name;
		// only what the server reads
		const form = {
			grand_total: frm.doc.grand_total,
			items: (frm.doc.items || []).map((row) => ({ against_sales_order: row.against_sales_order })),
		};
		return frappe
			.call({ method: API + "get_default_cod", type: "POST", args: { doc: JSON.stringify(form) } })
			.then((r) => {
				const amount = r && r.message;
				// another document is open now, or the person confirmed or typed an amount, or the carrier left Bosta
				if (typeof amount !== "number" || frm.doc.name !== name || !can_prefill(frm)) return;
				// the carrier, items or total changed meanwhile: ask once more
				if (cod_key(frm) !== key) return retried ? undefined : prefill_cod(frm, true);
				frm.__cod_prefilled = amount;
				const write = Number(frm.doc[COD_FIELD]) === amount ? Promise.resolve() : set_cod(frm, amount);
				return write.then(() => show_prefilled(frm, amount));
			})
			.catch(() => {
				// a convenience only: the server still computes the amount on save
			});
	}

	// Carrier changed away from Bosta: drop only the amount this code filled in and the person never touched.
	function clear_prefill(frm) {
		const filled = frm.__cod_prefilled;
		if (filled === undefined) return Promise.resolve();
		forget_prefill(frm);
		if (!is_editable(frm) || frm.doc.custom_cod_confirmed || Number(frm.doc[COD_FIELD]) !== filled) return Promise.resolve();
		return set_cod(frm, null);
	}

	// Only while the note is being created, and only when the server enabled the carrier.
	function sync_cod(frm) {
		ensure_state(frm);
		if (!carrier_enabled() || !is_creating(frm)) return Promise.resolve();
		return frm.doc.transporter_name === "Bosta" ? prefill_cod(frm) : clear_prefill(frm);
	}

	// Item, tax, discount and shipping edits come in bursts and ERPNext assigns `grand_total` without a field
	// event: ask once, after the burst. A timer of another document never writes into the open one.
	function schedule_sync(frm) {
		ensure_state(frm);
		if (!carrier_enabled() || !is_creating(frm)) return;
		clearTimeout(frm.__cod_timer);
		const name = frm.doc.name;
		frm.__cod_timer = setTimeout(() => {
			frm.__cod_timer = undefined;
			if (frm.doc && frm.doc.name === name) sync_cod(frm);
		}, PREFILL_DELAY_MS);
	}

	function refresh(frm) {
		ensure_state(frm);
		set_cod_state(frm);
		show_cod_hint(frm);
		// no longer new (saved): the text must not name an amount other than the saved one
		if (!is_creating(frm)) forget_prefill(frm);
		else if (can_prefill(frm)) prefill_cod(frm);
	}

	function onload(frm) {
		reset_state(frm);
	}

	function after_save(frm) {
		clearTimeout(frm.__cod_timer);
		frm.__cod_timer = undefined;
		forget_prefill(frm);
	}

	// Editing the COD is the person's choice: it confirms the amount.
	function custom_cod_amount(frm) {
		if (frm.__cod_programmatic) return;
		forget_prefill(frm);
		if (frm.doc.transporter_name === "Bosta" && is_editable(frm) && !frm.doc.custom_cod_confirmed) {
			frm.set_value("custom_cod_confirmed", 1);
		}
	}

	const on_change = (frm) => schedule_sync(frm);
	const form_events = {
		before_cancel,
		custom_cod_amount,
		onload,
		after_save,
		transporter: sync_cod,
		transporter_name: sync_cod,
		grand_total: on_change,
		items_remove: on_change,
		taxes_add: on_change,
		taxes_remove: on_change,
		taxes_and_charges: on_change,
		shipping_rule: on_change,
		additional_discount_percentage: on_change,
		discount_amount: on_change,
		apply_discount_on: on_change,
	};
	const item_events = {
		item_code: on_change,
		qty: on_change,
		rate: on_change,
		discount_percentage: on_change,
		discount_amount: on_change,
	};
	const tax_events = {
		charge_type: on_change,
		rate: on_change,
		tax_amount: on_change,
	};

	// ---- Cancel at Bosta ------------------------------------------------------------------------

	// Calls the server once. `done` is called when Bosta confirmed (or the note was already Cancelled);
	// `stop` when the cancel must not go on (a refusal, an unknown result or an error: a dialog says why).
	function cancel_at_bosta(frm, { done, stop }) {
		// The original `stop`: a retry must not wrap the wrapped one again.
		const original_stop = stop;
		const retry = () => cancel_at_bosta(frm, { done, stop: original_stop });
		// The server wrote the note (Pending / Unknown / Created again) even when the cancel stops here: load it,
		// so the next Save or Cancel does not fail with "modified after you opened it".
		stop = () => Promise.resolve(frm.reload_doc()).then(() => original_stop());
		// another carrier request is in flight: nothing is sent and the cancel does not go on
		return exclusive(
			frm,
			() =>
				frappe
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
					),
			original_stop
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
			exclusive(
				frm,
				() =>
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
							() =>
								Promise.resolve(frm.reload_doc()).then(() =>
									frm.doc.custom_integration_status === "Cancelled"
										? done({})
										: show_unknown(frm, __("Bosta still shows the shipment as not cancelled."), { done, stop, retry })
								),
							(error) => show_unknown(frm, message_of(error), { done, stop, retry })
						),
				stop
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
				done: () =>
					Promise.resolve(frm.reload_doc()).then(() => {
						// The note was already cancelled automatically (a Check read found Bosta's cancel): the
						// native cancel must not go on over a cancelled document.
						if (frm.doc.docstatus === 2) {
							frappe.validated = false;
							frappe.show_alert({ message: __("The Delivery Note was already cancelled."), indicator: "green" });
						}
						resolve();
					}),
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
					// one call at a time: the button stays off until the answer is back (and the note is locked
					// against any other carrier request)
					dialog.get_primary_btn().prop("disabled", true);
					return exclusive(
						frm,
						() =>
							frappe
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
								),
						() => dialog.get_primary_btn().prop("disabled", false)
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
				frm.perm &&
				frm.perm[0] &&
				frm.perm[0].write &&
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
			frappe.ui.form.on("Delivery Note", form_events);
			frappe.ui.form.on("Delivery Note Item", item_events);
			frappe.ui.form.on("Sales Taxes and Charges", tax_events);
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
			prefill_cod,
			sync_cod,
			schedule_sync,
			form_events,
			item_events,
			tax_events,
			onload,
			after_save,
			refresh,
			before_send,
			show_cod_hint,
			set_cod_state,
			cancel_shipment,
			edit_shipment,
			edit_error,
		};
	}
})();
