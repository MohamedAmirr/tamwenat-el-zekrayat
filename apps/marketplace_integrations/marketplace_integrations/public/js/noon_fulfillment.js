// Copyright (c) 2026, Tamwenat El Zekrayat and contributors
// For license information, please see license.txt

// Noon fulfillment client code for the Sales Order form (#146 decision 22, moved from the
// Marketplace Order form). sales_order.js loads this asset with frappe.require, only for a
// submitted Sales Order linked to a Noon Marketplace Order, so the dialogs live in exactly one
// place. Shipments are created, cancelled and refreshed from the Delivery Note (#415); what is
// left here is Mark Out of Stock, the order invoice and the stuck-reservation recovery. Every
// action calls the whitelisted api.noon_fulfillment endpoints with the linked Marketplace Order
// name; those endpoints still check write on that document. The buttons are only a convenience:
// they are hidden without that permission and the server remains the real gate.
//
// Which actions show is decided server-side by api.noon_fulfillment.get_available_actions (the
// same eligibility rules the actions enforce). The dialogs work from one "context" built per
// refresh: { marketplace_order, sales_order, reload }
//
// Every value sourced from the server (mp_item_nr, partner_sku) is Noon-sourced text and is
// escaped at the interpolation point, per docs/frappe-conventions.md.
//
// Wrapped in a function: every doctype_js file shares one scope with ERPNext's own Sales Order
// script, and this asset is loaded into that same page scope.
(function () {
	const API = "marketplace_integrations.api.noon_fulfillment.";

	// A context is rebuilt on every form refresh from the get_available_actions result.
	function build_context(frm) {
		return {
			marketplace_order: frm.doc.custom_marketplace_order,
			sales_order: frm.doc.name,
			reload: () => frm.reload_doc(),
		};
	}

	// Pure selection step, kept separate for testing: `available_actions` is the list of action
	// keys from get_available_actions.
	function select_noon_fulfillment_buttons(buttons, available_actions) {
		if (!Array.isArray(available_actions)) return buttons;
		return buttons.filter((button) => available_actions.includes(button.action));
	}

	function can_use_noon_fulfillment() {
		return Boolean(frappe.model.can_write("Marketplace Order"));
	}

	function noon_fulfillment_buttons(ctx) {
		return [
			{
				action: "mark_out_of_stock",
				label: __("Mark Out of Stock"),
				click: () => show_noon_mark_out_of_stock_dialog(ctx),
			},
			{
				action: "upload_order_invoice",
				label: __("Upload Order Invoice"),
				click: () => show_noon_upload_order_invoice_dialog(ctx),
			},
			{
				action: "retry_order_invoice",
				label: __("Retry Order Invoice"),
				click: () => run_noon_retry_order_invoice(ctx),
			},
			// #146 review finding #2: recovery action for an order-level Out of Stock reservation
			// orphaned by a hard-killed worker.
			{
				action: "release_stuck_oos_reservation",
				label: __("Release Stuck Out of Stock Reservation"),
				click: () => run_noon_release_stuck_oos_reservation(ctx),
			},
		];
	}

	function render_noon_fulfillment_buttons(frm, ctx, available_actions) {
		const group = __("Noon Fulfillment");
		const buttons = noon_fulfillment_buttons(ctx).filter(() => can_use_noon_fulfillment());
		select_noon_fulfillment_buttons(buttons, available_actions).forEach((button) =>
			frm.add_custom_button(button.label, button.click, group)
		);
	}

	// Loads the fulfillment state of the linked Noon order and adds the "Noon Fulfillment" button
	// group. Resolves with the state ({actions, cancelled_on_noon}) or null (no permission, a
	// failed call, or the form moved on); nothing is shown in those cases.
	function add_noon_fulfillment_actions(frm) {
		const sales_order = frm.doc.name;
		const marketplace_order = frm.doc.custom_marketplace_order;
		if (!frappe.model.can_read("Marketplace Order")) return Promise.resolve(null);
		const request = (frm.__mi_noon_request = (frm.__mi_noon_request || 0) + 1);
		return frappe
			.call({
				method: API + "get_available_actions",
				type: "POST",
				args: { marketplace_order },
				silent: true,
			})
			.then(
				(r) => {
					if (request !== frm.__mi_noon_request || frm.doc.name !== sales_order) return null;
					const state = r.message || {};
					render_noon_fulfillment_buttons(frm, build_context(frm), state.actions || []);
					if (state.cancelled_on_noon && frm.dashboard && frm.dashboard.set_headline) {
						frm.dashboard.set_headline(
							__("Cancelled on Noon; no fulfillment actions available"),
							"orange"
						);
					}
					return state;
				},
				() => null
			);
	}

	// {queued: bool} is the shape every api.noon_fulfillment endpoint that enqueues returns (a
	// deduplicated job_id), so the "already queued" toast applies to all of them.
	function show_noon_queue_toast(r, ctx) {
		const queued = (r.message || {}).queued;
		frappe.show_alert({
			message: queued
				? __("Noon fulfillment action queued. Refresh this document to see the results.")
				: __("A Noon fulfillment action for this order is already queued or running."),
			indicator: "blue",
		});
		ctx.reload();
	}

	function fetch_noon_shippable_units(ctx) {
		return frappe
			.call({
				method: API + "get_shippable_units",
				args: { marketplace_order: ctx.marketplace_order },
			})
			.then((r) => r.message || []);
	}

	// mp_item_nr/partner_sku are untrusted Noon-sourced text (#146 decision 9's ignore_xss_filter
	// fields) - escaped here before being used as a MultiCheck option label, which (like other
	// Frappe controls outside the Data/Text escape whitelist, see docs/frappe-conventions.md)
	// does not escape labels itself.
	//
	// The option `value` is this unit's own array index (a string Frappe itself generates, never
	// Noon-sourced text): multicheck.js interpolates a control's `value` unescaped into a
	// `data-unit="..."` HTML attribute, so a raw mp_item_nr could break out and run script.
	// mp_item_nrs_from_unit_indices() maps the selected indices back to the real `mp_item_nr`s.
	//
	// The visible label is "Item name (SKU)" (just the SKU for an unmapped SKU); a SKU shared by
	// several units gets a "(1 of 3)" counter so the lines can be told apart. The raw mp_item_nr
	// is only in the tooltip (title attribute), never visible text.
	function noon_unit_checklist_options(units) {
		const escape = frappe.utils.escape_html;
		const totals = {};
		units.forEach((unit) => {
			totals[unit.partner_sku || ""] = (totals[unit.partner_sku || ""] || 0) + 1;
		});
		const seen = {};
		return units.map((unit, index) => {
			const sku = unit.partner_sku || "";
			seen[sku] = (seen[sku] || 0) + 1;
			let text = unit.item_name && sku ? `${unit.item_name} (${sku})` : unit.item_name || sku;
			if (totals[sku] > 1) text += ` (${__("{0} of {1}", [seen[sku], totals[sku]])})`;
			return {
				label: `<span title="${escape(unit.mp_item_nr || "")}">${escape(text)}</span>`,
				value: String(index),
			};
		});
	}

	// Maps a MultiCheck's selected index-string values back to the real `mp_item_nr`s the Noon
	// fulfillment API endpoints expect, using the exact same `units` array the dialog's options
	// were built from.
	function mp_item_nrs_from_unit_indices(units, selected_indices) {
		return (selected_indices || [])
			.map((index) => (units[Number(index)] || {}).mp_item_nr)
			.filter((mp_item_nr) => mp_item_nr);
	}

	// Marking Out of Stock after the Sales Order exists is a divergence risk, so warn and require
	// confirmation before proceeding. On the Sales Order form the Sales Order is always there
	// (#131: it is submitted immediately on sync).
	function noon_confirm_if_submitted_sales_order(ctx, proceed) {
		if (!ctx.sales_order) {
			proceed();
			return;
		}
		frappe.confirm(
			__(
				"ERPNext Sales Order {0} is linked to this order and may be updated or cancelled (or its undelivered remainder closed) when ERPNext and the marketplace stage allow it; otherwise the order is flagged in Needs Attention. Continue?",
				[frappe.utils.escape_html(ctx.sales_order)]
			),
			proceed
		);
	}

	function show_noon_mark_out_of_stock_dialog(ctx) {
		fetch_noon_shippable_units(ctx).then((units) => {
			if (!units.length) {
				frappe.msgprint({
					message: __("No units are currently eligible to mark Out of Stock."),
					indicator: "orange",
				});
				return;
			}
			const dialog = new frappe.ui.Dialog({
				title: __("Mark Units Out of Stock"),
				fields: [
					{
						fieldname: "units",
						fieldtype: "MultiCheck",
						label: __("Units"),
						options: noon_unit_checklist_options(units),
						columns: 1,
						reqd: 1,
					},
				],
				primary_action_label: __("Mark Out of Stock"),
				primary_action(values) {
					if (!values.units || !values.units.length) {
						frappe.msgprint({
							message: __("At least one unit must be selected."),
							indicator: "red",
						});
						return;
					}
					dialog.hide();
					noon_confirm_if_submitted_sales_order(ctx, () => {
						frappe
							.call({
								method: API + "mark_out_of_stock",
								args: {
									marketplace_order: ctx.marketplace_order,
									mp_item_nrs: mp_item_nrs_from_unit_indices(units, values.units),
								},
								freeze: true,
								freeze_message: __("Queuing Out of Stock update..."),
							})
							.then((r) => show_noon_queue_toast(r, ctx));
					});
				},
			});
			dialog.show();
		});
	}

	function show_noon_upload_order_invoice_dialog(ctx) {
		const dialog = new frappe.ui.Dialog({
			title: __("Upload Noon Order Invoice"),
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
				frappe
					.call({
						method: API + "upload_order_invoice",
						args: {
							marketplace_order: ctx.marketplace_order,
							file_url: values.invoice_file,
							invoice_nr: values.invoice_nr,
							issued_at: values.issued_at,
						},
						freeze: true,
						freeze_message: __("Queuing invoice upload..."),
					})
					.then((r) => show_noon_queue_toast(r, ctx));
			},
		});
		dialog.show();
	}

	function run_noon_retry_order_invoice(ctx) {
		frappe
			.call({
				method: API + "retry_order_invoice",
				args: { marketplace_order: ctx.marketplace_order },
				freeze: true,
				freeze_message: __("Queuing retry..."),
			})
			.then((r) => show_noon_queue_toast(r, ctx));
	}

	// release_stuck_oos_reservation() runs synchronously (no Noon HTTP call, so nothing to
	// enqueue), so it does not follow the queued/already-queued toast pattern.
	function run_noon_release_stuck_oos_reservation(ctx) {
		frappe.confirm(
			__(
				"This clears any pending Noon Out of Stock reservation on this order, in case a worker died before releasing it. Continue?"
			),
			() => {
				frappe
					.call({
						method: API + "release_stuck_oos_reservation",
						args: { marketplace_order: ctx.marketplace_order },
						freeze: true,
						freeze_message: __("Releasing reservation..."),
					})
					.then(() => {
						frappe.show_alert({
							message: __("Reservation released."),
							indicator: "green",
						});
						ctx.reload();
					});
			}
		);
	}

	const exported = {
		add_noon_fulfillment_actions,
		build_context,
		can_use_noon_fulfillment,
		noon_fulfillment_buttons,
		noon_unit_checklist_options,
		mp_item_nrs_from_unit_indices,
		select_noon_fulfillment_buttons,
		show_noon_mark_out_of_stock_dialog,
		show_noon_upload_order_invoice_dialog,
		run_noon_release_stuck_oos_reservation,
	};

	if (typeof frappe !== "undefined") {
		frappe.marketplace_integrations = frappe.marketplace_integrations || {};
		frappe.marketplace_integrations.noon_fulfillment = exported;
	}

	if (typeof module !== "undefined") {
		module.exports = exported;
	}
})();
