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

	// Unverified at Noon: its API docs say CancelShipment cancels all items of the shipment too, and the order sync
	// then cancels or shrinks the Sales Order by itself.
	// A submitted note is then held (#552): it stops at Awaiting Return with the stock still out until Confirm Returned.
	const cancel_warning = (frm) =>
		__(
			"Cancel the Noon shipment of this Delivery Note? Noon documents that cancelling the shipment also cancels its items at Noon: the Sales Order lines may be cancelled or reduced, and the units may not be shippable again. This cannot be undone."
		) +
		(frm && frm.doc && frm.doc.docstatus === 1
			? " " +
			  __(
					"The Delivery Note will stop at Awaiting Return with its stock still out until a Noon User presses Confirm Returned."
			  )
			: "");

	function escape(text) {
		return frappe.utils.escape_html(text || "");
	}

	// The message of a rejected frappe.call(), plain text (the shell parses the server HTML).
	function message_of(error) {
		const shipping = frappe.marketplace_integrations && frappe.marketplace_integrations.shipping;
		return (shipping && shipping.server_message && shipping.server_message(error)) || "";
	}

	// One carrier request per Delivery Note at a time (the shell holds the lock); runs `fn` directly without it.
	function exclusive(frm, fn, on_busy) {
		const shipping = frappe.marketplace_integrations && frappe.marketplace_integrations.shipping;
		return shipping && shipping.exclusive ? shipping.exclusive(frm, fn, on_busy) : Promise.resolve(fn());
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
			message: __("Type the AWB, or tick Generate AWB through Noon, before creating the shipment."),
			indicator: "orange",
		});
		return false;
	}

	function refresh(frm) {
		// the saved state (refresh runs after load and save): the live checkbox and AWB cannot tell a generated AWB from a typed one
		frm.__noon_awb_generated = awb_generated(frm);
		set_awb_state(frm);
		show_invoice_intro(frm);
		// after the shell's own intro, so it replaces "Cancelled at Noon. Press Cancel ..." (a held note is not cancelled by hand)
		show_held_intro(frm);
	}

	// Ticking the box means Noon generates the AWB: a typed AWB is cleared (the server refuses both).
	function custom_noon_generate_awb(frm) {
		if (frm.doc.transporter_name !== CARRIER || !is_editable(frm) || frm.__noon_awb_generated) return;
		if (frm.doc.custom_noon_generate_awb && (frm.doc.lr_no || "").trim()) frm.set_value("lr_no", "");
		set_awb_state(frm);
	}

	// ---- Cancel at Noon -------------------------------------------------------------------------

	// Calls the server once. `done` is called when Noon confirmed (or the note was already Cancelled);
	// `stop` when the cancel must not go on (a refusal, an unknown result or an error: a dialog says why).
	function cancel_at_noon(frm, { done, stop }) {
		// The original `stop`: a retry must not wrap the wrapped one again.
		const original_stop = stop;
		const retry = () => cancel_at_noon(frm, { done, stop: original_stop });
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
					),
			original_stop
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
			exclusive(
				frm,
				() =>
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
						),
				stop
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
			const stop = () => {
				frappe.validated = false;
				resolve();
			};
			// Our own warning, asked once here (the native confirmation says nothing about Noon): Noon documents
			// that cancelling the shipment also cancels its items. Declining ends the cancel.
			frappe.confirm(
				cancel_warning(frm),
				() =>
					cancel_at_noon(frm, {
						// the server changed the note: load it before the native cancel saves it
						done: () =>
							Promise.resolve(frm.reload_doc()).then(() => {
								// The note was already cancelled automatically (a Check read found Noon's cancel): the
								// native cancel must not go on over a cancelled document.
								if (frm.doc.docstatus === 2) {
									frappe.validated = false;
									frappe.show_alert({ message: __("The Delivery Note was already cancelled."), indicator: "green" });
								} else if (is_held(frm)) {
									// #552: the shipment is cancelled and the note is held (Awaiting Return): the native cancel
									// is refused until Confirm Returned, so it must not go on.
									frappe.validated = false;
									frappe.show_alert({
										message: __("The Noon shipment was cancelled. Use Confirm Returned when the goods are back."),
										indicator: "orange",
									});
								}
								resolve();
							}),
						stop,
					}),
				stop
			);
		});
	}

	// Form hook for the Delivery Note Lifecycle workflow's Cancel action (Status menu), which does not go through
	// before_cancel: without this the server guard refuses the cancel of a note with a live shipment (a Shipped note in
	// Ready for Pickup included) and the form offers no other way out. Frappe applies the workflow action after this
	// hook whatever frappe.validated says (form/workflow.js), so the only way to stop it is a promise that never
	// settles; the screen it froze is released here. The server guard (guard_cancel_or_delete) stays the safety net.
	function before_workflow_action(frm) {
		if (frm.selected_workflow_action !== "Cancel") return;
		if (frm.doc.transporter_name !== CARRIER || !CANCELLABLE.includes(frm.doc.custom_integration_status || "")) return;
		const unfreeze = () => {
			if (frappe.dom && frappe.dom.unfreeze) frappe.dom.unfreeze();
		};
		const halt = () => {
			frm.selected_workflow_action = null;
			unfreeze();
			return new Promise(() => {});
		};
		unfreeze();
		return new Promise((resolve) => {
			frappe.confirm(
				cancel_warning(frm),
				() =>
					cancel_at_noon(frm, {
						done: () =>
							Promise.resolve(frm.reload_doc()).then(() => {
								// Already cancelled (a Check read found Noon's cancel): the workflow must not go on.
								if (frm.doc.docstatus === 2) {
									frappe.show_alert({ message: __("The Delivery Note was already cancelled."), indicator: "green" });
									return halt();
								}
								if (is_held(frm)) {
									// #552: held (Awaiting Return): the workflow Cancel must not go on until Confirm Returned.
									frappe.show_alert({
										message: __("The Noon shipment was cancelled. Use Confirm Returned when the goods are back."),
										indicator: "orange",
									});
									return halt();
								}
								if (frappe.dom && frappe.dom.freeze) frappe.dom.freeze();
								resolve();
							}),
						stop: halt,
					}),
				halt
			);
		});
	}

	// A draft note with a live shipment cannot be deleted until the shipment is cancelled at Noon.
	function cancel_shipment(frm) {
		return new Promise((resolve) => {
			frappe.confirm(
				cancel_warning(frm),
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

	// "Upload Shipment Invoice": a submitted note whose shipment is Created. The dialog is shared with the order
	// invoice (noon_invoice.js); the server refuses anything else (and a non-FBPI warehouse). It never blocks
	// Submit or Cancel.
	function upload_shipment_invoice(frm) {
		const invoice = frappe.marketplace_integrations && frappe.marketplace_integrations.noon_invoice;
		if (!invoice) return;
		invoice.start({
			doctype: "Delivery Note",
			name: frm.doc.name,
			title: __("Upload Shipment Invoice"),
			uploaded_on: frm.doc.custom_noon_invoice_uploaded_on,
			send: (values, key) =>
				frappe.call({
					method: "marketplace_integrations.api.noon_fulfillment.upload_delivery_note_invoice",
					type: "POST",
					args: {
						delivery_note: frm.doc.name,
						file_url: values.invoice_file,
						invoice_nr: values.invoice_nr,
						issued_at: values.issued_at,
						idempotency_key: key || null,
					},
					// the refusal is shown by the shared dialog (parsed and escaped), not by frappe.call
					silent: true,
				}),
			reload: () => frm.reload_doc(),
		});
	}

	// The upload endpoint needs write on the Delivery Note: others do not see the button.
	function can_write(frm) {
		return Boolean(frm.perm && frm.perm[0] && frm.perm[0].write);
	}

	// ---- Confirm Returned (#527) -----------------------------------------------------------------

	const RETURN_API = "marketplace_integrations.api.noon_fulfillment.";

	// The shipment of a submitted note was cancelled (by Noon or by us), at any stage: the note stays submitted until Confirm Returned.
	function is_held(frm) {
		return Boolean(
			frm.doc &&
				frm.doc.docstatus === 1 &&
				frm.doc.custom_integration_status === "Cancelled" &&
				frm.doc.custom_shipping_status === "Awaiting Return"
		);
	}

	// The endpoints need Noon User or System Manager (no Delivery Note write right is needed).
	function can_confirm_returned() {
		const user = frappe.user;
		return Boolean(user && user.has_role && (user.has_role("Noon User") || user.has_role("System Manager")));
	}

	function show_held_intro(frm) {
		if (!is_held(frm) || !frm.set_intro) return;
		frm.set_intro(
			__(
				"The Noon shipment was cancelled. The stock stays out until a Noon User confirms the goods are back (Confirm Returned)."
			),
			"orange"
		);
	}

	function show_error(error, fallback) {
		frappe.msgprint({
			message: escape(message_of(error) || fallback),
			indicator: "red",
			title: __("Noon shipment"),
		});
	}

	// Returned Qty is editable only while the write-off box is ticked; unticking puts every line back to "all returned".
	function sync_returned_editable(dialog) {
		const on = Boolean(dialog.get_value("write_off"));
		const field = dialog.fields_dict.lines;
		if (field && field.grid && field.grid.update_docfield_property) {
			field.grid.update_docfield_property("returned_qty", "read_only", on ? 0 : 1);
		}
		if (!on && field && field.df && field.df.data) {
			field.df.data.forEach((row) => (row.returned_qty = row.shipped_qty));
		}
		if (field && field.grid && field.grid.refresh) field.grid.refresh();
	}

	function submit_return(frm, dialog, values) {
		const write_off = values.write_off ? 1 : 0;
		const lines = (values.lines || []).map((row) => ({
			name: row.name,
			returned_qty: write_off ? row.returned_qty : row.shipped_qty,
		}));
		return exclusive(frm, () =>
			frappe
				.call({
					method: RETURN_API + "confirm_returned",
					type: "POST",
					args: { delivery_note: frm.doc.name, lines: JSON.stringify(lines), write_off },
					freeze: true,
					freeze_message: __("Confirming the return..."),
					// the server's refusal is shown below (parsed and escaped), not by frappe.call
					silent: true,
				})
				.then(
					() => {
						dialog.hide();
						frappe.show_alert({
							message: __("Returned goods confirmed. The Delivery Note was cancelled."),
							indicator: "green",
						});
						return frm.reload_doc();
					},
					(error) => show_error(error, __("The return could not be confirmed."))
				)
		);
	}

	function open_return_dialog(frm, lines) {
		const dialog = new frappe.ui.Dialog({
			title: __("Confirm Returned"),
			fields: [
				{
					fieldtype: "Table",
					fieldname: "lines",
					cannot_add_rows: 1,
					cannot_delete_rows: 1,
					in_place_edit: 1,
					data: lines.map((line) => ({
						name: line.name,
						item_code: line.item_code,
						item_name: line.item_name,
						shipped_qty: line.shipped_qty,
						returned_qty: line.shipped_qty,
					})),
					fields: [
						{ fieldtype: "Data", fieldname: "item_code", label: __("Item"), read_only: 1, in_list_view: 1, columns: 3 },
						{ fieldtype: "Data", fieldname: "item_name", label: __("Item Name"), read_only: 1, in_list_view: 1, columns: 3 },
						{ fieldtype: "Float", fieldname: "shipped_qty", label: __("Shipped Qty"), read_only: 1, in_list_view: 1, columns: 2 },
						{ fieldtype: "Float", fieldname: "returned_qty", label: __("Returned Qty"), read_only: 1, in_list_view: 1, columns: 2 },
					],
				},
				{
					fieldtype: "Check",
					fieldname: "write_off",
					label: __("Create Material Issue for broken/missing units"),
					onchange: () => sync_returned_editable(dialog),
				},
			],
			primary_action_label: __("Confirm Returned"),
			primary_action: (values) => submit_return(frm, dialog, values),
		});
		dialog.show();
		return dialog;
	}

	// "Confirm Returned" button: reads the lines (the server refuses a note that is not held), then asks.
	function confirm_returned(frm) {
		return frappe
			.call({
				method: RETURN_API + "get_return_lines",
				type: "POST",
				args: { delivery_note: frm.doc.name },
				freeze: true,
				freeze_message: __("Reading the lines..."),
				silent: true,
			})
			.then(
				(r) => {
					const lines = r.message || [];
					if (!lines.length) {
						frappe.msgprint({
							message: escape(__("This Delivery Note has no stock lines to confirm.")),
							indicator: "orange",
							title: __("Noon shipment"),
						});
						return;
					}
					return open_return_dialog(frm, lines);
				},
				(error) => show_error(error, __("The lines could not be read."))
			);
	}

	// Its own form handler, not the shell's add_buttons hook: the shell shows carrier buttons only to a user with
	// Delivery Note write, and Confirm Returned needs a Noon User with read only to see it.
	function add_return_button(frm) {
		if (frm.doc.transporter_name === CARRIER && is_held(frm) && can_confirm_returned()) {
			frm.add_custom_button(__("Confirm Returned"), () => confirm_returned(frm));
		}
	}

	function add_buttons(frm) {
		if (frm.doc.docstatus === 0 && CANCELLABLE.includes(frm.doc.custom_integration_status || "")) {
			frm.add_custom_button(__("Cancel Noon Shipment"), () => cancel_shipment(frm));
		}
		if (frm.doc.docstatus === 1 && frm.doc.custom_integration_status === "Created" && can_write(frm)) {
			frm.add_custom_button(__("Upload Shipment Invoice"), () => upload_shipment_invoice(frm));
		}
	}

	// Intro under the Shipping section: when the invoice was uploaded.
	function show_invoice_intro(frm) {
		if (frm.doc.docstatus === 1 && frm.doc.custom_noon_invoice_uploaded_on && frm.dashboard && frm.dashboard.add_indicator) {
			frm.dashboard.add_indicator(
				__("Invoice uploaded on {0}", [frappe.utils.escape_html(frappe.datetime.str_to_user(frm.doc.custom_noon_invoice_uploaded_on))]),
				"green"
			);
		}
	}

	const hooks = { before_send, refresh, add_buttons };

	if (typeof frappe !== "undefined" && frappe.marketplace_integrations && frappe.marketplace_integrations.shipping) {
		frappe.marketplace_integrations.shipping.register_carrier(CARRIER, hooks);
		if (frappe.ui && frappe.ui.form) {
			frappe.ui.form.on("Delivery Note", {
				before_cancel,
				before_workflow_action,
				custom_noon_generate_awb,
				refresh: add_return_button,
			});
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
			before_workflow_action,
			cancel_shipment,
			add_buttons,
			upload_shipment_invoice,
			is_held,
			add_return_button,
			confirm_returned,
			open_return_dialog,
			submit_return,
			sync_returned_editable,
			show_held_intro,
		};
	}
})();
