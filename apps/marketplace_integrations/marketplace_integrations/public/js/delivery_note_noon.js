// Copyright (c) 2026, Tamwenat El Zekrayat and contributors
// For license information, please see license.txt

// Noon part of the Delivery Note Shipping section (epic #413, #415). delivery_note_shipping.js owns the
// shared behaviour (intro, "Sending to Noon...", Check and Refresh buttons); this file registers Noon with it
// and adds the Noon-only pieces: the AWB rules (type the AWB, or tick "Generate AWB through Noon" under it,
// which makes the AWB read-only), the locked fields, and cancelling the shipment at Noon before the native
// cancel (and the "Cancel Noon Shipment" button of a draft note, whose delete the server refuses until then).
// Every rule is enforced server side (integrations/noon/delivery_note_shipment.py).
//
// Wrapped in a function: doctype_js files share one scope with ERPNext's own Delivery Note script.
(function () {
	const CARRIER = "Noon";
	// request, check, refresh and cancel are neutral
	const CARRIER_API = "marketplace_integrations.api.carrier_shipments.";
	const EDITABLE = ["", "Not Created", "Creation Failed"];
	// Integration Statuses with a Noon shipment that must be cancelled at Noon before ERPNext cancels.
	const CANCELLABLE = ["Created", "Cancellation Pending", "Cancellation Unknown"];

	function escape(text) {
		return frappe.utils.escape_html(text || "");
	}

	// The message of a rejected frappe.call(), plain text (the shell parses the server HTML).
	function message_of(error) {
		const shipping = frappe.marketplace_integrations && frappe.marketplace_integrations.shipping;
		return (shipping && shipping.server_message && shipping.server_message(error)) || "";
	}

	function is_editable(frm) {
		return Boolean(frm.doc && frm.doc.docstatus === 0 && EDITABLE.includes(frm.doc.custom_integration_status || ""));
	}

	// An AWB Noon generated: the checkbox is ticked and the AWB is stored. It cannot be changed any more.
	function awb_generated(frm) {
		return Boolean(frm.doc.custom_noon_generate_awb && (frm.doc.lr_no || "").trim());
	}

	// The carrier is always Noon; the checkbox and the AWB lock once the AWB was generated or the shipment
	// started; a ticked checkbox makes the AWB read-only (Noon fills it during Submit).
	function set_awb_state(frm) {
		const open = is_editable(frm) && !awb_generated(frm);
		frm.set_df_property("transporter", "read_only", 1);
		frm.set_df_property("custom_noon_generate_awb", "read_only", open ? 0 : 1);
		frm.set_df_property("lr_no", "read_only", open && !frm.doc.custom_noon_generate_awb ? 0 : 1);
	}

	// Submit gate: an AWB is typed or Noon is asked for one. Returns false (after saying why) to stop the Submit.
	function before_send(frm) {
		if ((frm.doc.lr_no || "").trim() || frm.doc.custom_noon_generate_awb) return true;
		frappe.msgprint({
			message: __("Type the AWB, or tick Generate AWB through Noon, before submitting."),
			indicator: "orange",
		});
		return false;
	}

	function refresh(frm) {
		set_awb_state(frm);
	}

	// Ticking the box means Noon generates the AWB: a typed AWB is cleared (the server refuses both).
	function custom_noon_generate_awb(frm) {
		if (frm.doc.transporter_name !== CARRIER || !is_editable(frm) || awb_generated(frm)) return;
		if (frm.doc.custom_noon_generate_awb && (frm.doc.lr_no || "").trim()) frm.set_value("lr_no", "");
		set_awb_state(frm);
	}

	// ---- Cancel at Noon -------------------------------------------------------------------------

	// Calls the server once. `done` is called when Noon confirmed (or the note was already Cancelled);
	// `stop` when the cancel must not go on (a refusal, an unknown result or an error: a dialog says why).
	function cancel_at_noon(frm, { done, stop }) {
		const retry = () => cancel_at_noon(frm, { done, stop });
		return frappe
			.call({
				method: CARRIER_API + "cancel_shipment",
				type: "POST",
				args: { delivery_note: frm.doc.name },
				freeze: true,
				freeze_message: __("Cancelling at Noon..."),
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
						message: escape(message_of(error) || __("The shipment could not be cancelled at Noon.")),
						indicator: "red",
						title: __("Noon shipment"),
					});
					return stop();
				}
			);
	}

	function show_refused(message, stop) {
		frappe.msgprint({
			message: `<p>${__("Noon refused: {0}", [escape(message)])}</p><p class="text-muted">${escape(
				__("Nothing was cancelled in ERPNext.")
			)}</p>`,
			indicator: "red",
			title: __("Noon shipment"),
		});
		return stop();
	}

	// Timeout, a temporary error or an answer that does not settle it: nothing was cancelled in ERPNext.
	// Retry asks Noon again (it reads the order first); Check with Noon only reads the state; closing ends the attempt.
	function show_unknown(frm, message, { done, stop, retry }) {
		let acted = false;
		const check = () =>
			frappe
				.call({
					method: CARRIER_API + "refresh_shipment",
					type: "POST",
					args: { delivery_note: frm.doc.name },
					freeze: true,
					freeze_message: __("Checking with Noon..."),
					silent: true,
				})
				.then(
					() =>
						Promise.resolve(frm.reload_doc()).then(() =>
							frm.doc.custom_integration_status === "Cancelled"
								? done({})
								: show_unknown(frm, __("Noon still shows the shipment as not cancelled."), { done, stop, retry })
						),
					(error) => show_unknown(frm, message_of(error), { done, stop, retry })
				);
		const dialog = new frappe.ui.Dialog({
			title: __("Noon shipment"),
			fields: [{ fieldtype: "HTML", fieldname: "body" }],
			primary_action_label: __("Retry"),
			primary_action: () => {
				acted = true;
				dialog.hide();
				return retry();
			},
			secondary_action_label: __("Check with Noon"),
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
			`<p>${escape(__("Noon did not confirm the cancellation. Nothing was cancelled in ERPNext."))}</p>` +
				(message ? `<p class="text-muted">${escape(message)}</p>` : "")
		);
		return dialog;
	}

	// Form hook: runs after the native "Permanently Cancel" confirmation. Resolves when the native cancel
	// may go on; sets frappe.validated = false (and shows why) when it must not.
	function before_cancel(frm) {
		if (frm.doc.transporter_name !== CARRIER || !CANCELLABLE.includes(frm.doc.custom_integration_status || "")) return;
		return new Promise((resolve) => {
			cancel_at_noon(frm, {
				// the server changed the note: load it before the native cancel saves it
				done: () => Promise.resolve(frm.reload_doc()).then(() => resolve()),
				stop: () => {
					frappe.validated = false;
					resolve();
				},
			});
		});
	}

	// A draft note with a live shipment cannot be deleted until the shipment is cancelled at Noon.
	function cancel_shipment(frm) {
		return new Promise((resolve) => {
			frappe.confirm(
				__("Cancel the Noon shipment of this Delivery Note? This cannot be undone."),
				() =>
					cancel_at_noon(frm, {
						done: () =>
							Promise.resolve(frm.reload_doc()).then(() => {
								frappe.show_alert({
									message: __("The Noon shipment was cancelled. The Delivery Note can now be deleted."),
									indicator: "green",
								});
								resolve();
							}),
						stop: () => resolve(),
					}),
				() => resolve()
			);
		});
	}

	function add_buttons(frm) {
		if (frm.doc.docstatus === 0 && CANCELLABLE.includes(frm.doc.custom_integration_status || "")) {
			frm.add_custom_button(__("Cancel Noon Shipment"), () => cancel_shipment(frm));
		}
	}

	const hooks = { before_send, refresh, add_buttons };

	if (typeof frappe !== "undefined" && frappe.marketplace_integrations && frappe.marketplace_integrations.shipping) {
		frappe.marketplace_integrations.shipping.register_carrier(CARRIER, hooks);
		if (frappe.ui && frappe.ui.form) {
			frappe.ui.form.on("Delivery Note", { before_cancel, custom_noon_generate_awb });
		}
	}

	if (typeof module !== "undefined") {
		module.exports = {
			hooks,
			before_send,
			refresh,
			set_awb_state,
			awb_generated,
			custom_noon_generate_awb,
			before_cancel,
			cancel_shipment,
			add_buttons,
		};
	}
})();
