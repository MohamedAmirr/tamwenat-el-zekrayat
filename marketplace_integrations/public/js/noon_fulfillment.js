// Copyright (c) 2026, Tamwenat El Zekrayat and contributors
// For license information, please see license.txt

// Noon fulfillment client code for the Sales Order form (#146 decision 22, moved from the
// Marketplace Order form). sales_order.js loads this asset with frappe.require, only for a
// submitted Sales Order linked to a Noon Marketplace Order, so the dialogs live in exactly one
// place. Every action calls the whitelisted api.noon_fulfillment endpoints with the linked
// Marketplace Order name; those endpoints still check write on that document, and Mark Handed
// Over still needs create on Delivery Note. The buttons are only a convenience: they are hidden
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

	// Mark Handed Over drafts a Delivery Note, so it also needs create permission on that doctype.
	function can_use_noon_fulfillment(action) {
		if (!frappe.model.can_write("Marketplace Order")) return false;
		if (action === "mark_handed_over") return Boolean(frappe.model.can_create("Delivery Note"));
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

	// Compact read-only list of the shipments for the Marketplace Order tab. Empty string when
	// there is nothing to show.
	function noon_shipments_html(shipments) {
		if (!Array.isArray(shipments) || !shipments.length) return "";
		const escape = frappe.utils.escape_html;
		const rows = shipments
			.map((shipment) => {
				const awbs = (shipment.awbs || [])
					.map((awb) => escape([awb.courier, awb.awb_nr].filter(Boolean).join(" ")))
					.join(", ");
				// Every Delivery Note created for the shipment (the first one alone for older data).
				const notes = (shipment.delivery_notes || []).length
					? shipment.delivery_notes
					: shipment.delivery_note
					? [shipment.delivery_note]
					: [];
				const delivery_note = notes
					.map(
						(name) =>
							`<a href="/app/delivery-note/${encodeURIComponent(name)}">${escape(name)}</a>`
					)
					.join(", ");
				const unresolved = (shipment.unresolved || []).map((text) => escape(text)).join("; ");
				return `<tr><td>${escape(shipment.integration_shipment_nr || "")}</td><td>${escape(
					__(shipment.status || "")
				)}</td><td>${awbs}</td><td>${delivery_note}</td><td>${unresolved}</td></tr>`;
			})
			.join("");
		return (
			`<h5>${escape(__("Noon Shipments"))}</h5>` +
			`<table class="table table-bordered"><thead><tr><th>${escape(__("Shipment"))}</th><th>${escape(
				__("Status")
			)}</th><th>${escape(__("AWB"))}</th><th>${escape(__("Delivery Note"))}</th><th>${escape(
				__("Not added")
			)}</th></tr></thead><tbody>${rows}</tbody></table>`
		);
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
	function noon_unit_checklist_options(units) {
		return units.map((unit, index) => ({
			label: `${frappe.utils.escape_html(unit.mp_item_nr)} (${frappe.utils.escape_html(
				unit.partner_sku || ""
			)})`,
			value: String(index),
		}));
	}

	// Maps a MultiCheck's selected index-string values back to the real `mp_item_nr`s the Noon
	// fulfillment API endpoints expect, using the exact same `units` array the dialog's options
	// were built from.
	function mp_item_nrs_from_unit_indices(units, selected_indices) {
		return (selected_indices || [])
			.map((index) => (units[Number(index)] || {}).mp_item_nr)
			.filter((mp_item_nr) => mp_item_nr);
	}

	function shipment_option(shipment) {
		return {
			label: frappe.utils.escape_html(shipment.integration_shipment_nr),
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
						fieldname: "awb_source",
						fieldtype: "Select",
						label: __("AWB Source"),
						options: "Noon\nOwn Courier",
						default: "Noon",
						reqd: 1,
					},
					{
						fieldname: "courier",
						fieldtype: "Data",
						label: __("Courier"),
						depends_on: "eval:doc.awb_source=='Own Courier'",
					},
					{
						fieldname: "awb_nr",
						fieldtype: "Data",
						label: __("AWB Nr"),
						depends_on: "eval:doc.awb_source=='Own Courier'",
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
					if (values.awb_source === "Own Courier" && (!values.courier || !values.awb_nr)) {
						frappe.msgprint({
							message: __("Courier and AWB Nr are required for Own Courier shipments."),
							indicator: "red",
						});
						return;
					}
					dialog.hide();
					frappe
						.call({
							method: API + "create_shipment",
							args: {
								marketplace_order: ctx.marketplace_order,
								mp_item_nrs: mp_item_nrs_from_unit_indices(units, values.units),
								awb_source: values.awb_source,
								courier: values.courier,
								awb_nr: values.awb_nr,
							},
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

	// #368: Created shipments not handed over yet (no live Delivery Note, no recorded handover), or with
	// unresolved handover units.
	function noon_handover_shipment_options(ctx) {
		return (ctx.shipments || [])
			.filter(
				(shipment) =>
					shipment.status === "Created" &&
					(!shipment.handed_over || (shipment.unresolved || []).length)
			)
			.map(shipment_option);
	}

	// Pure step for testing: the alert shown for the mark_handed_over result
	// ({delivery_note, created, unresolved[{text, reason}]}). Nothing here is submitted and no
	// Noon call is made. Units that could not be drafted stay on the shipment and keep the order
	// in Needs Action (or Syncing while a Sales Order sync is pending).
	function noon_handover_alert(result) {
		result = result || {};
		const unresolved = result.unresolved || [];
		// entry.text carries Noon-sourced SKUs: escaped here, the alert renders HTML.
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
		return {
			message:
				(result.created
					? __("Draft Delivery Note {0} created. Review and submit it.", [escape(result.delivery_note)])
					: result.added_to_existing
					? __("Units added to the draft Delivery Note {0}. Review and submit it.", [
							escape(result.delivery_note),
					  ])
					: __("Delivery Note {0} already exists for this shipment.", [escape(result.delivery_note)])) +
				skipped,
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
							"This creates one draft Delivery Note for the shipment's units. Nothing is sent to Noon and the Delivery Note is not submitted."
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
						freeze_message: __("Creating the draft Delivery Note..."),
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
				{ fieldname: "courier", fieldtype: "Data", label: __("Courier"), reqd: 1 },
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
		noon_shipments_html,
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
