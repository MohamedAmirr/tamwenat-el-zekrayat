// Copyright (c) 2026, Tamwenat El Zekrayat and contributors
// For license information, please see license.txt

// Noon fulfillment client code for the Sales Order form (#146 decision 22, moved from the
// Marketplace Order form). sales_order.js loads this asset with frappe.require, only for a
// submitted Sales Order linked to a Noon Marketplace Order, so the dialogs live in exactly one
// place. Every action calls the whitelisted api.noon_fulfillment endpoints with the linked
// Marketplace Order name; those endpoints still check write on that document, and Mark Handed
// Over still needs submit on Delivery Note (it submits the shipment's note). The buttons are only a convenience: they are hidden
// without those permissions and the server remains the real gate.
//
// Which actions show is decided server-side by api.noon_fulfillment.get_available_actions (the
// same eligibility rules the actions enforce). That call also returns the shipments, so the
// dialogs and the read-only summary work from one "context" built per refresh:
//   { marketplace_order, sales_order, shipments, reload }
//
// Every value sourced from the server (mp_item_nr, partner_sku, shipment numbers, AWBs, skipped
// unit text) is Noon-sourced text and is escaped at the interpolation point, per
// docs/frappe-conventions.md.
//
// Wrapped in a function: every doctype_js file shares one scope with ERPNext's own Sales Order
// script, and this asset is loaded into that same page scope.
(function () {
	const API = "marketplace_integrations.api.noon_fulfillment.";
	// Noon is the only courier; the value Noon documents for its own AWBs.
	const NOON_COURIER = "noon";

	// A context is rebuilt on every form refresh from the get_available_actions result.
	function build_context(frm, state) {
		return {
			marketplace_order: frm.doc.custom_marketplace_order,
			sales_order: frm.doc.name,
			shipments: (state && state.shipments) || [],
			reload: () => frm.reload_doc(),
		};
	}

	// Pure selection step, kept separate for testing: `available_actions` is the list of action
	// keys from get_available_actions.
	function select_noon_fulfillment_buttons(buttons, available_actions) {
		if (!Array.isArray(available_actions)) return buttons;
		return buttons.filter((button) => available_actions.includes(button.action));
	}

	// Mark Handed Over submits the shipment's Delivery Note, so it also needs submit permission on that doctype.
	function can_use_noon_fulfillment(action) {
		if (!frappe.model.can_write("Marketplace Order")) return false;
		if (action === "mark_handed_over") return Boolean(frappe.model.can_submit("Delivery Note"));
		return true;
	}

	function noon_fulfillment_buttons(ctx) {
		return [
			{
				action: "create_shipment",
				label: __("Create Shipment"),
				click: () => show_noon_create_shipment_dialog(ctx),
			},
			{
				action: "mark_out_of_stock",
				label: __("Mark Out of Stock"),
				click: () => show_noon_mark_out_of_stock_dialog(ctx),
			},
			{
				action: "cancel_shipment",
				label: __("Cancel Shipment"),
				click: () => show_noon_cancel_shipment_dialog(ctx),
			},
			// #376: cancels every Created shipment on Noon so the Sales Order can then be cancelled.
			{
				action: "cancel_on_noon",
				label: __("Cancel on Noon"),
				click: () => show_noon_cancel_on_noon_dialog(ctx),
			},
			{ action: "add_awb", label: __("Add AWB"), click: () => show_noon_add_awb_dialog(ctx) },
			{
				action: "mark_handed_over",
				label: __("Mark Handed Over"),
				click: () => show_noon_mark_handed_over_dialog(ctx),
			},
			{
				action: "upload_invoice",
				label: __("Upload Invoice"),
				click: () => show_noon_upload_invoice_dialog(ctx),
			},
			{
				action: "upload_order_invoice",
				label: __("Upload Order Invoice"),
				click: () => show_noon_upload_order_invoice_dialog(ctx),
			},
			{ action: "retry", label: __("Retry"), click: () => show_noon_retry_dialog(ctx) },
			{
				action: "retry_order_invoice",
				label: __("Retry Order Invoice"),
				click: () => run_noon_retry_order_invoice(ctx),
			},
			// #146 review finding #2: recovery actions for a shipment stuck in Error or Cancel
			// Pending with no other way out, and for an order-level Out of Stock reservation
			// orphaned by a hard-killed worker.
			{
				action: "discard_shipment",
				label: __("Discard Shipment"),
				click: () => show_noon_discard_shipment_dialog(ctx),
			},
			{
				action: "release_stuck_oos_reservation",
				label: __("Release Stuck Out of Stock Reservation"),
				click: () => run_noon_release_stuck_oos_reservation(ctx),
			},
		];
	}

	function render_noon_fulfillment_buttons(frm, ctx, available_actions) {
		const group = __("Noon Fulfillment");
		const buttons = noon_fulfillment_buttons(ctx).filter((button) =>
			can_use_noon_fulfillment(button.action)
		);
		select_noon_fulfillment_buttons(buttons, available_actions).forEach((button) =>
			frm.add_custom_button(button.label, button.click, group)
		);
	}

	// Loads the fulfillment state of the linked Noon order and adds the "Noon Fulfillment" button
	// group. Resolves with the state ({actions, cancelled_on_noon, shipments, ...}) or null (no
	// permission, a failed call, or the form moved on); nothing is shown in those cases.
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
					render_noon_fulfillment_buttons(frm, build_context(frm, state), state.actions || []);
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

	// "INTEGRATION_ITEM_STATUS_SHIPPED" -> "Shipped": Noon's own unit status, shown as Noon sends it
	// (not translated: it is Noon vocabulary), empty when the unit is not in the payload.
	function noon_unit_status_text(status) {
		const text = String(status || "")
			.replace(/^INTEGRATION_ITEM_STATUS_/, "")
			.replace(/_/g, " ")
			.toLowerCase();
		return text.charAt(0).toUpperCase() + text.slice(1);
	}

	// A Delivery Note or Sales Invoice link with its draft/submitted state; the server only sends a
	// name the user may read (#378), so a missing document renders as nothing.
	function noon_document_link(route, document) {
		if (!document || !document.name) return "";
		const escape = frappe.utils.escape_html;
		return `<a href="/app/${route}/${encodeURIComponent(document.name)}">${escape(document.name)}</a> (${escape(
			__(document.state || "Draft")
		)})`;
	}

	function noon_shipment_block_html(shipment) {
		const escape = frappe.utils.escape_html;
		const awbs = (shipment.awbs || [])
			.map((awb) => escape([awb.courier === "noon" ? "Noon" : awb.courier, awb.awb_nr].filter(Boolean).join(" ")))
			.join(", ");
		const notes = (shipment.delivery_notes || []).map((note) => noon_document_link("delivery-note", note)).join(", ");
		const facts = [
			`${escape(__("AWB"))}: ${awbs || "-"}`,
			`${escape(__("Delivery Note"))}: ${notes || "-"}`,
			escape(__("{0} of {1} units shipped", [shipment.shipped_count || 0, shipment.unit_count || 0])),
		].join(" &middot; ");
		const note_error = shipment.delivery_note_error
			? `<p class="text-danger">${escape(shipment.delivery_note_error)}</p>`
			: "";
		const unresolved = (shipment.unresolved || []).length
			? `<p>${escape(__("Not added"))}: ${shipment.unresolved.map((text) => escape(text)).join("; ")}</p>`
			: "";
		const rows = (shipment.units || [])
			.map(
				(unit) =>
					`<tr><td>${escape(unit.item_name || "")}</td><td>${escape(unit.sku || "")}</td><td>${escape(
						unit.qty
					)}</td><td>${escape(noon_unit_status_text(unit.status))}</td><td>${escape(
						unit.on_note ? __("Yes") : __("No")
					)}</td></tr>`
			)
			.join("");
		const table = rows
			? `<table class="table table-bordered"><thead><tr><th>${escape(__("Item"))}</th><th>${escape(
					__("SKU")
				)}</th><th>${escape(__("Qty"))}</th><th>${escape(__("Noon Status"))}</th><th>${escape(
					__("On the Delivery Note")
				)}</th></tr></thead><tbody>${rows}</tbody></table>`
			: `<p class="text-muted">${escape(__("No units."))}</p>`;
		return (
			`<h5 title="${escape(shipment.integration_shipment_nr || "")}">${escape(
				noon_shipment_label(shipment.integration_shipment_nr)
			)} <span class="indicator-pill grey">${escape(__(shipment.status || ""))}</span></h5>` +
			`<p>${facts}</p>${note_error}${unresolved}${table}`
		);
	}

	// Content of the Sales Order's Noon Fulfillment tab (#378): the Sales Invoice, then one block
	// per shipment. `state` is the get_available_actions result, or null when it could not be
	// loaded (no permission, draft Sales Order, a failed call).
	function noon_fulfillment_tab_html(state) {
		const escape = frappe.utils.escape_html;
		if (!state) return `<p class="text-muted">${escape(__("Noon fulfillment is not available for this order."))}</p>`;
		const invoice = noon_document_link("sales-invoice", state.sales_invoice);
		const invoice_html = `<h5>${escape(__("Sales Invoice"))}</h5><p>${
			invoice || `<span class="text-muted">${escape(__("No Sales Invoice yet."))}</span>`
		}</p>`;
		const shipments = state.shipments || [];
		const shipments_html = shipments.length
			? shipments.map(noon_shipment_block_html).join("")
			: `<p class="text-muted">${escape(__("No shipments yet."))}</p>`;
		return invoice_html + shipments_html;
	}

	// {queued: bool} is the shape every api.noon_fulfillment endpoint that enqueues returns (a
	// deduplicated job_id), so the "already queued" toast applies to all of them. reload() picks
	// up the Pending shipment/unit/AWB row(s) each request path writes and commits before
	// enqueuing the worker, even though the worker itself hasn't run yet.
	function show_noon_queue_toast(r, ctx) {
		const queued = (r.message || {}).queued;
		frappe.show_alert({
			message: queued
				? __("Noon fulfillment action queued. Refresh this document to see the results.")
				: __("A Noon fulfillment action for this shipment is already queued or running."),
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

	// "Shipment 2" for "<order nr>-2"; the full number stays in tooltips and as the dropdown
	// value (it is the key the API takes).
	function noon_shipment_label(shipment_nr) {
		const nr = String(shipment_nr || "");
		return __("Shipment {0}", [nr.slice(nr.lastIndexOf("-") + 1)]);
	}

	function shipment_option(shipment) {
		return {
			label: frappe.utils.escape_html(
				`${noon_shipment_label(shipment.integration_shipment_nr)} - ${__(shipment.status || "")}`
			),
			value: shipment.integration_shipment_nr,
		};
	}

	function noon_created_shipment_options(ctx) {
		return (ctx.shipments || [])
			.filter((shipment) => shipment.status === "Created")
			.map(shipment_option);
	}

	// Marking Out of Stock or Cancelling a shipment after the Sales Order exists is a divergence
	// risk, so warn and require confirmation before proceeding. On the Sales Order form the Sales
	// Order is always there (#131: it is submitted immediately on sync).
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

	function show_noon_create_shipment_dialog(ctx) {
		fetch_noon_shippable_units(ctx).then((units) => {
			if (!units.length) {
				frappe.msgprint({
					message: __("No units are currently shippable for this order."),
					indicator: "orange",
				});
				return;
			}
			const dialog = new frappe.ui.Dialog({
				title: __("Create Noon Shipment"),
				fields: [
					{
						fieldname: "units",
						fieldtype: "MultiCheck",
						label: __("Units"),
						options: noon_unit_checklist_options(units),
						columns: 1,
						reqd: 1,
					},
					{
						fieldname: "awb_nr",
						fieldtype: "Data",
						label: __("AWB Nr"),
						description: __(
							"Type or scan the AWB on the Noon sticker. Leave empty to use a Noon AWB."
						),
					},
				],
				primary_action_label: __("Create"),
				primary_action(values) {
					if (!values.units || !values.units.length) {
						frappe.msgprint({
							message: __("At least one unit must be selected."),
							indicator: "red",
						});
						return;
					}
					const args = {
						marketplace_order: ctx.marketplace_order,
						mp_item_nrs: mp_item_nrs_from_unit_indices(units, values.units),
					};
					const awb_nr = (values.awb_nr || "").trim();
					if (awb_nr) args.awb_nr = awb_nr;
					dialog.hide();
					frappe
						.call({
							method: API + "create_shipment",
							args,
							freeze: true,
							freeze_message: __("Queuing shipment creation..."),
						})
						.then((r) => show_noon_queue_toast(r, ctx));
				},
			});
			dialog.show();
		});
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

	function show_noon_cancel_shipment_dialog(ctx) {
		const options = noon_created_shipment_options(ctx);
		if (!options.length) {
			frappe.msgprint({
				message: __("No cancellable (Created) shipments on this order."),
				indicator: "orange",
			});
			return;
		}
		const dialog = new frappe.ui.Dialog({
			title: __("Cancel Noon Shipment"),
			fields: [
				{
					fieldname: "integration_shipment_nr",
					fieldtype: "Select",
					label: __("Shipment"),
					options,
					reqd: 1,
				},
			],
			primary_action_label: __("Cancel Shipment"),
			primary_action(values) {
				dialog.hide();
				noon_confirm_if_submitted_sales_order(ctx, () => {
					frappe
						.call({
							method: API + "cancel_shipment",
							args: {
								marketplace_order: ctx.marketplace_order,
								integration_shipment_nr: values.integration_shipment_nr,
							},
							freeze: true,
							freeze_message: __("Queuing shipment cancellation..."),
						})
						.then((r) => show_noon_queue_toast(r, ctx));
				});
			},
		});
		dialog.show();
	}

	// #376: HTML of the confirmation. Everything from the server is Noon-sourced text and escaped.
	function noon_cancel_on_noon_html(preview) {
		const escape = frappe.utils.escape_html;
		const rows = (preview.created || [])
			.map((shipment) => {
				const items = (shipment.items || [])
					.map((item) => `${escape(item.label)} x ${escape(item.qty)}`)
					.join(", ");
				return `<li>${escape(noon_shipment_label(shipment.integration_shipment_nr))}${
					items ? ` - ${items}` : ""
				}</li>`;
			})
			.join("");
		const skipped = (preview.skipped || [])
			.map((shipment) => noon_shipment_label(shipment.integration_shipment_nr))
			.join(", ");
		return (
			`<p>${escape(__("These Created shipments will be cancelled on Noon. Noon also cancels their units."))}</p>` +
			`<ul>${rows}</ul>` +
			(skipped
				? `<p>${escape(
						__("Not cancelled because a Delivery Note is live: {0}. Cancel that Delivery Note first.", [skipped])
				  )}</p>`
				: "") +
			`<p>${escape(__("Units that are not in a shipment are not changed on Noon."))}</p>` +
			`<p>${escape(
				__("The Sales Order cancel completes only after Noon confirms: when every shipment shows Cancelled, cancel the Sales Order again.")
			)}</p>`
		);
	}

	// Pure step for testing: the alert for the cancel_created_shipments result
	// ({queued, skipped, failed[{integration_shipment_nr, error}]}). Errors are escaped, the alert renders HTML.
	function noon_cancel_on_noon_alert(result) {
		result = result || {};
		const escape = frappe.utils.escape_html;
		const failed = result.failed || [];
		if (failed.length) {
			const details = failed
				.map((entry) => `${escape(entry.integration_shipment_nr)}: ${escape(entry.error)}`)
				.join("; ");
			return {
				message: __("Queued {0} shipment(s). Not queued: {1}", [(result.queued || []).length, details]),
				indicator: "orange",
			};
		}
		return {
			message: __(
				"Cancellation of {0} shipment(s) queued on Noon. Cancel the Sales Order after they show Cancelled.",
				[(result.queued || []).length]
			),
			indicator: "blue",
		};
	}

	function show_noon_cancel_on_noon_dialog(ctx) {
		frappe
			.call({
				method: API + "get_cancel_on_noon_preview",
				args: { marketplace_order: ctx.marketplace_order },
			})
			.then((r) => {
				const preview = r.message || {};
				if (!(preview.created || []).length) {
					frappe.msgprint({
						message: __("No Created shipment can be cancelled on Noon."),
						indicator: "orange",
					});
					return;
				}
				const dialog = new frappe.ui.Dialog({
					title: __("Cancel on Noon"),
					fields: [{ fieldtype: "HTML", fieldname: "preview", options: noon_cancel_on_noon_html(preview) }],
					primary_action_label: __("Cancel on Noon"),
					primary_action() {
						dialog.hide();
						frappe
							.call({
								method: API + "cancel_created_shipments",
								args: { marketplace_order: ctx.marketplace_order },
								freeze: true,
								freeze_message: __("Queuing shipment cancellation..."),
							})
							.then((result) => {
								frappe.show_alert(noon_cancel_on_noon_alert(result.message));
								ctx.reload();
							});
					},
					secondary_action_label: __("Not now"),
					secondary_action() {
						dialog.hide();
					},
				});
				dialog.show();
			});
	}

	// #368, #390: Created shipments with a draft Delivery Note to submit or units no live Delivery
	// Note covers yet (decided by the server: can_mark_handed_over).
	function noon_handover_shipment_options(ctx) {
		return (ctx.shipments || [])
			.filter((shipment) => shipment.status === "Created" && shipment.can_mark_handed_over)
			.map(shipment_option);
	}

	// Pure step for testing: the alert shown for the mark_handed_over result
	// ({delivery_note, created, added_to_existing, submitted, submit_error, unresolved[{text, reason}]}).
	// No Noon call is made. Units that could not be put on the note stay on the shipment and keep
	// the order in Needs Action (or Syncing while a Sales Order sync is pending).
	function noon_handover_alert(result) {
		result = result || {};
		const unresolved = result.unresolved || [];
		// entry.text and submit_error carry Noon-sourced text: escaped here, the alert renders HTML.
		const escape = frappe.utils.escape_html;
		let skipped = unresolved.length
			? " " +
			  __("Not added: {0}", [unresolved.map((entry) => escape(entry.text)).join("; ")])
			: "";
		if (unresolved.some((entry) => entry.reason === "excess" || entry.reason === "not_on_order")) {
			skipped +=
				" " +
				__(
					"The Sales Order is behind the platform order: a sync was queued and these units are retried afterwards."
				);
		}
		if (!result.delivery_note) {
			return {
				message: __("No Delivery Note was created.") + skipped,
				indicator: "orange",
			};
		}
		if (result.submit_error) {
			return {
				message:
					__("Delivery Note {0} could not be submitted and stays a draft: {1}", [
						escape(result.delivery_note),
						escape(result.submit_error),
					]) + skipped,
				indicator: "red",
			};
		}
		return {
			message:
				__("Delivery Note {0} submitted.", [escape(result.delivery_note)]) + skipped,
			indicator: skipped ? "orange" : "green",
		};
	}

	function show_noon_mark_handed_over_dialog(ctx) {
		const options = noon_handover_shipment_options(ctx);
		if (!options.length) {
			frappe.msgprint({
				message: __("No Created shipments on this order are waiting to be marked handed over."),
				indicator: "orange",
			});
			return;
		}
		const dialog = new frappe.ui.Dialog({
			title: __("Mark Noon Shipment Handed Over"),
			fields: [
				{
					fieldname: "note",
					fieldtype: "HTML",
					options: `<p class="text-muted">${frappe.utils.escape_html(
						__(
							"This puts every unit of the shipment that is not on a Delivery Note yet on the shipment's Delivery Note and submits it, so stock leaves the warehouse in ERPNext. Nothing is sent to Noon."
						)
					)}</p>`,
				},
				{
					fieldname: "integration_shipment_nr",
					fieldtype: "Select",
					label: __("Shipment"),
					options,
					reqd: 1,
				},
			],
			primary_action_label: __("Mark Handed Over"),
			primary_action(values) {
				dialog.hide();
				frappe
					.call({
						method: API + "mark_handed_over",
						args: {
							marketplace_order: ctx.marketplace_order,
							integration_shipment_nr: values.integration_shipment_nr,
						},
						freeze: true,
						freeze_message: __("Submitting the Delivery Note..."),
					})
					.then((r) => {
						frappe.show_alert(noon_handover_alert(r.message));
						ctx.reload();
					});
			},
		});
		dialog.show();
	}

	function show_noon_add_awb_dialog(ctx) {
		const options = noon_created_shipment_options(ctx);
		if (!options.length) {
			frappe.msgprint({
				message: __("No Created shipments on this order to add an AWB to."),
				indicator: "orange",
			});
			return;
		}
		const dialog = new frappe.ui.Dialog({
			title: __("Add Noon Shipment AWB"),
			fields: [
				{
					fieldname: "integration_shipment_nr",
					fieldtype: "Select",
					label: __("Shipment"),
					options,
					reqd: 1,
				},
				{
					// Noon is the only courier; the stored/sent value is Noon's documented lowercase "noon".
					fieldname: "courier",
					fieldtype: "Select",
					label: __("Courier"),
					options: [{ label: __("Noon"), value: NOON_COURIER }],
					default: NOON_COURIER,
					reqd: 1,
				},
				{ fieldname: "awb_nr", fieldtype: "Data", label: __("AWB Nr"), reqd: 1 },
			],
			primary_action_label: __("Add AWB"),
			primary_action(values) {
				dialog.hide();
				frappe
					.call({
						method: API + "add_awb",
						args: {
							marketplace_order: ctx.marketplace_order,
							integration_shipment_nr: values.integration_shipment_nr,
							courier: values.courier,
							awb_nr: values.awb_nr,
						},
						freeze: true,
						freeze_message: __("Queuing AWB addition..."),
					})
					.then((r) => show_noon_queue_toast(r, ctx));
			},
		});
		dialog.show();
	}

	function show_noon_upload_invoice_dialog(ctx) {
		const options = noon_created_shipment_options(ctx);
		if (!options.length) {
			frappe.msgprint({
				message: __("No Created shipments on this order to upload an invoice for."),
				indicator: "orange",
			});
			return;
		}
		const dialog = new frappe.ui.Dialog({
			title: __("Upload Noon Shipment Invoice"),
			fields: [
				{
					fieldname: "integration_shipment_nr",
					fieldtype: "Select",
					label: __("Shipment"),
					options,
					reqd: 1,
				},
				{
					fieldname: "invoice_file",
					fieldtype: "Attach",
					label: __("Invoice PDF"),
					reqd: 1,
					// #146 review finding #9: upload_invoice() rejects a non-private File
					// server-side (the authoritative check - frappe.ui.Dialog's Attach control
					// has no supported way to hide or force the uploader's own Private
					// checkbox), but tell the user up front so a public upload is a deliberate,
					// informed exception rather than a surprise rejection after the dialog closes.
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
						method: API + "upload_invoice",
						args: {
							marketplace_order: ctx.marketplace_order,
							integration_shipment_nr: values.integration_shipment_nr,
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

	function show_noon_upload_order_invoice_dialog(ctx) {
		const dialog = new frappe.ui.Dialog({
			title: __("Upload Noon Order Invoice"),
			fields: [
				{
					fieldname: "invoice_file",
					fieldtype: "Attach",
					label: __("Invoice PDF"),
					reqd: 1,
					// Same as the shipment invoice dialog: the server rejects a non-private File;
					// this just tells the user up front.
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

	function show_noon_retry_dialog(ctx) {
		const options = (ctx.shipments || []).map(shipment_option);
		if (!options.length) {
			frappe.msgprint({ message: __("No shipments on this order."), indicator: "orange" });
			return;
		}
		const dialog = new frappe.ui.Dialog({
			title: __("Retry Noon Shipment Action"),
			fields: [
				{
					fieldname: "integration_shipment_nr",
					fieldtype: "Select",
					label: __("Shipment"),
					options,
					reqd: 1,
				},
			],
			primary_action_label: __("Retry"),
			primary_action(values) {
				dialog.hide();
				frappe
					.call({
						method: API + "retry",
						args: {
							marketplace_order: ctx.marketplace_order,
							integration_shipment_nr: values.integration_shipment_nr,
						},
						freeze: true,
						freeze_message: __("Queuing retry..."),
					})
					.then((r) => show_noon_queue_toast(r, ctx));
			},
		});
		dialog.show();
	}

	// #146 review finding #2: discard_shipment()/release_stuck_oos_reservation() run
	// synchronously (no Noon HTTP call, so nothing to enqueue) - unlike every other Noon
	// Fulfillment action above, these do not follow the queued/already-queued toast pattern.
	function noon_discardable_shipment_options(ctx) {
		return (ctx.shipments || [])
			.filter((shipment) => ["Error", "Cancel Pending"].includes(shipment.status))
			.map(shipment_option);
	}

	function show_noon_discard_shipment_dialog(ctx) {
		const options = noon_discardable_shipment_options(ctx);
		if (!options.length) {
			frappe.msgprint({
				message: __("No shipments on this order are in Error or Cancel Pending."),
				indicator: "orange",
			});
			return;
		}
		const dialog = new frappe.ui.Dialog({
			title: __("Discard Noon Shipment"),
			fields: [
				{
					fieldname: "integration_shipment_nr",
					fieldtype: "Select",
					label: __("Shipment"),
					options,
					reqd: 1,
				},
			],
			primary_action_label: __("Discard"),
			primary_action(values) {
				dialog.hide();
				frappe.confirm(
					__(
						"Shipment {0} will be marked Discarded and its units released for a new shipment. This never calls Noon - continue?",
						[frappe.utils.escape_html(values.integration_shipment_nr)]
					),
					() => {
						frappe
							.call({
								method: API + "discard_shipment",
								args: {
									marketplace_order: ctx.marketplace_order,
									integration_shipment_nr: values.integration_shipment_nr,
								},
								freeze: true,
								freeze_message: __("Discarding shipment..."),
							})
							.then(() => {
								frappe.show_alert({
									message: __("Shipment discarded."),
									indicator: "green",
								});
								ctx.reload();
							});
					}
				);
			},
		});
		dialog.show();
	}

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
		noon_fulfillment_tab_html,
		noon_unit_status_text,
		noon_unit_checklist_options,
		mp_item_nrs_from_unit_indices,
		noon_created_shipment_options,
		noon_discardable_shipment_options,
		noon_handover_shipment_options,
		noon_handover_alert,
		select_noon_fulfillment_buttons,
		show_noon_create_shipment_dialog,
		show_noon_mark_handed_over_dialog,
		show_noon_cancel_shipment_dialog,
		show_noon_cancel_on_noon_dialog,
		noon_cancel_on_noon_html,
		noon_cancel_on_noon_alert,
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
