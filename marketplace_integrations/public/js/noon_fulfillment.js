// Copyright (c) 2026, Tamwenat El Zekrayat and contributors
// For license information, please see license.txt

// Noon fulfillment client code for the Sales Order form (#146 decision 22, moved from the
// Marketplace Order form). sales_order.js loads this asset with frappe.require, only for a
// submitted Sales Order linked to a Noon Marketplace Order, so the dialogs live in exactly one
// place. Shipments are created, cancelled and refreshed from the Delivery Note (#415); what is
// left here is Mark as Out of Stock (a button on the Sales Order item row form, #416, which needs
// write on the Sales Order only), the order invoice and the stuck-reservation recovery. The
// invoice and recovery actions call the whitelisted api.noon_fulfillment endpoints with the linked
// Marketplace Order name; those endpoints still check write on that document. The buttons are
// only a convenience: they are hidden without that permission and the server remains the real gate.
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

	// The order-invoice buttons need write on the Sales Order only (no Marketplace Order right); the
	// stuck-reservation recovery still needs write on the Marketplace Order.
	const INVOICE_ACTIONS = ["upload_order_invoice", "retry_order_invoice"];

	function noon_fulfillment_buttons(ctx) {
		return [
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
		const buttons = noon_fulfillment_buttons(ctx).filter((button) =>
			INVOICE_ACTIONS.includes(button.action)
				? frappe.model.can_write("Sales Order")
				: can_use_noon_fulfillment()
		);
		select_noon_fulfillment_buttons(buttons, available_actions).forEach((button) =>
			frm.add_custom_button(button.label, button.click, group)
		);
	}

	// Asks the server which order-invoice buttons apply (write on the Sales Order only) and adds them.
	// Resolves with the state ({can_upload, can_retry, uploaded}) or null.
	function add_noon_invoice_actions(frm) {
		if (!frm.doc || frm.doc.docstatus !== 1 || !frappe.model.can_write("Sales Order")) {
			return Promise.resolve(null);
		}
		const sales_order = frm.doc.name;
		return frappe
			.call({ method: API + "get_order_invoice_state", type: "POST", args: { sales_order }, silent: true })
			.then(
				(r) => {
					if (frm.doc.name !== sales_order) return null;
					const state = r.message || {};
					const ctx = Object.assign(build_context(frm), { order_invoice_uploaded: Boolean(state.uploaded) });
					const actions = [];
					if (state.can_upload) actions.push("upload_order_invoice");
					if (state.can_retry) actions.push("retry_order_invoice");
					render_noon_fulfillment_buttons(frm, ctx, actions);
					return state;
				},
				() => null
			);
	}

	// Loads the fulfillment state of the linked Noon order and adds the "Noon Fulfillment" button
	// group. Resolves with the state ({actions, cancelled_on_noon}) or null (no permission, a
	// failed call, or the form moved on); nothing is shown in those cases.
	function add_noon_fulfillment_actions(frm) {
		// the row action needs only write on the Sales Order: it runs before the Marketplace Order gate
		add_noon_out_of_stock_rows(frm);
		add_noon_invoice_actions(frm);
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

	// ---- Mark as Out of Stock: one Sales Order item row (#416) -------------------------------------
	//
	// The action is the "Mark as Out of Stock" Button field of the Sales Order Item row form
	// (Custom Field, hidden until shown here). The server decides which rows offer it
	// (get_out_of_stock_rows) and refuses the rest. Flow: confirm (final at Noon), then the
	// "Sending to Noon..." dialog that polls check_mark_out_of_stock until the job wrote an outcome.
	// Every text that came from Noon is escaped where shown.
	const timing = { poll_ms: 2000, max_wait_ms: 60000 };
	const OOS_BUTTON = "custom_noon_mark_out_of_stock";

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

	function confirm_noon_out_of_stock(frm, row_name) {
		frappe.confirm(
			frappe.utils.escape_html(
				__(
					"This marks the Item Out of Stock on the whole order: every row of this Item is removed, not only this row. It is final at Noon: it cannot be fulfilled later even if it is restocked, and Noon may charge cancellation fees. Continue?"
				)
			),
			() => send_noon_out_of_stock(frm, row_name)
		);
	}

	// Shows the result dialog of a press that ended without success. `retry_label` runs the same press
	// again: the server asks Noon first when the earlier press may have reached it.
	function show_noon_out_of_stock_problem(frm, row_name, html, retry_label) {
		frappe.msgprint({
			title: __("Mark as Out of Stock"),
			message: html,
			indicator: "red",
			primary_action: {
				label: retry_label,
				action: () => {
					if (frappe.hide_msgprint) frappe.hide_msgprint();
					send_noon_out_of_stock(frm, row_name);
				},
			},
		});
	}

	function send_noon_out_of_stock(frm, row_name) {
		const sales_order = frm.doc.name;
		const escape = frappe.utils.escape_html;
		let dialog = null;
		let timer = null;
		let settled = false;
		const finish = (state, message, keep_sending) => {
			if (settled) return;
			settled = true;
			if (timer) clearTimeout(timer);
			if (dialog) dialog.hide();
			if (keep_sending) {
				frappe.show_alert({
					message: escape(__("Still sending to Noon. Open the Sales Order later to see the result.")),
					indicator: "blue",
				});
			} else if (state === "done") {
				frappe.show_alert({
					message: escape(__("The item was marked Out of Stock at Noon and removed from the order.")),
					indicator: "green",
				});
				frm.reload_doc();
			} else if (state === "unknown") {
				show_noon_out_of_stock_problem(
					frm,
					row_name,
					`<p>${escape(__("It is not known whether Noon marked the item Out of Stock."))}</p><p>${escape(message || "")}</p><p class="text-muted">${escape(__("Check Noon asks Noon first and never sends the request twice."))}</p>`,
					__("Check Noon")
				);
			} else if (state === "local_failed") {
				show_noon_out_of_stock_problem(
					frm,
					row_name,
					`<p>${escape(__("Noon marked the item Out of Stock, but the Sales Order was not updated."))}</p><p>${escape(message || "")}</p><p class="text-muted">${escape(__("Retry only repeats the update here; Noon is not asked to do it again."))}</p>`,
					__("Retry")
				);
			} else {
				show_noon_out_of_stock_problem(
					frm,
					row_name,
					`<p>${escape(message || __("The request could not be completed."))}</p><p class="text-muted">${escape(__("The item stays in the order."))}</p>`,
					__("Retry")
				);
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
		dialog.fields_dict.body.$wrapper.html(`<p>${escape(__("Please wait. The item is removed from the order once Noon confirms."))}</p>`);

		const started = Date.now();
		const poll = (request_id) =>
			frappe
				.call({
					method: API + "check_mark_out_of_stock",
					type: "POST",
					args: { sales_order, request_id },
					silent: true,
				})
				.then(
					(r) => {
						const result = (r && r.message) || {};
						if (result.state && result.state !== "pending") return finish(result.state, result.message);
						if (Date.now() - started >= timing.max_wait_ms) return finish("pending", "", true);
						timer = setTimeout(() => poll(request_id), timing.poll_ms);
					},
					() => finish("pending", "", true)
				);
		return frappe
			.call({
				method: API + "mark_item_out_of_stock",
				type: "POST",
				args: { sales_order, sales_order_item: row_name },
				// the refusal is shown below (parsed and escaped), not by frappe.call
				silent: true,
			})
			.then(
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
					return poll(result.request_id);
				},
				(error) => {
					if (settled) return;
					settled = true;
					if (dialog) dialog.hide();
					frappe.msgprint({
						title: __("Mark as Out of Stock"),
						message: escape(server_message(error) || __("The request could not be sent.")),
						indicator: "red",
					});
				}
			);
	}

	// Asks the server which item rows offer the action and keeps the answer on the form for the row form.
	// Needs write on the Sales Order only (no Marketplace Order permission). Resolves with the row names.
	function add_noon_out_of_stock_rows(frm) {
		frm.__mi_noon_oos_rows = [];
		if (!frappe.model.can_write("Sales Order") || !frm.doc || frm.doc.docstatus !== 1) {
			return Promise.resolve([]);
		}
		const sales_order = frm.doc.name;
		return frappe
			.call({ method: API + "get_out_of_stock_rows", type: "POST", args: { sales_order }, silent: true })
			.then(
				(r) => {
					if (frm.doc.name !== sales_order) return [];
					frm.__mi_noon_oos_rows = Array.isArray(r.message) ? r.message : [];
					return frm.__mi_noon_oos_rows;
				},
				() => []
			);
	}

	// form_render of a Sales Order Item row: show the button only for a row the server offered.
	function on_noon_row_render(frm, cdt, cdn) {
		const grid_row = frm.fields_dict.items && frm.fields_dict.items.grid.grid_rows_by_docname[cdn];
		if (!grid_row) return;
		grid_row.toggle_display(OOS_BUTTON, (frm.__mi_noon_oos_rows || []).includes(cdn));
	}

	function on_noon_row_button(frm, cdt, cdn) {
		if ((frm.__mi_noon_oos_rows || []).includes(cdn)) confirm_noon_out_of_stock(frm, cdn);
	}

	// The order invoice uses the dialog shared with the shipment invoice (noon_invoice.js): a new press has
	// no key, a Retry passes one (any value: the server reuses the key saved on the Marketplace Order).
	function order_invoice_options(ctx) {
		return {
			doctype: "Sales Order",
			name: ctx.sales_order,
			title: __("Upload Noon Order Invoice"),
			uploaded: ctx.order_invoice_uploaded,
			send: (values, key) =>
				frappe.call({
					method: API + (key ? "retry_order_invoice" : "upload_order_invoice"),
					type: "POST",
					args: key
						? { sales_order: ctx.sales_order }
						: {
								sales_order: ctx.sales_order,
								file_url: values.invoice_file,
								invoice_nr: values.invoice_nr,
								issued_at: values.issued_at,
							},
					// the refusal is shown by the shared dialog (parsed and escaped), not by frappe.call
					silent: true,
				}),
			reload: ctx.reload,
		};
	}

	function show_noon_upload_order_invoice_dialog(ctx) {
		const invoice = frappe.marketplace_integrations && frappe.marketplace_integrations.noon_invoice;
		if (invoice) invoice.start(order_invoice_options(ctx));
	}

	function run_noon_retry_order_invoice(ctx) {
		const invoice = frappe.marketplace_integrations && frappe.marketplace_integrations.noon_invoice;
		if (invoice) invoice.send(order_invoice_options(ctx), {}, "retry");
	}

	// The server queues a job that asks Noon first: units Noon already shows Out of Stock stay reserved.
	function run_noon_release_stuck_oos_reservation(ctx) {
		frappe.confirm(
			__(
				"This asks Noon which reserved units are already Out of Stock and frees the rest, in case a worker died before releasing them. It runs in the background. Continue?"
			),
			() => {
				frappe
					.call({
						method: API + "release_stuck_oos_reservation",
						args: { marketplace_order: ctx.marketplace_order },
						freeze: true,
						freeze_message: __("Queueing..."),
					})
					.then((r) => {
							const queued = Boolean(r && r.message && r.message.queued);
						frappe.show_alert({
							message: queued
									? __("Release queued. Reload the order in a moment.")
									: __("A Noon fulfillment action for this order is already queued or running."),
							indicator: queued ? "green" : "blue",
						});
					});
			}
		);
	}

	const exported = {
		add_noon_fulfillment_actions,
		add_noon_invoice_actions,
		run_noon_retry_order_invoice,
		build_context,
		can_use_noon_fulfillment,
		noon_fulfillment_buttons,
		select_noon_fulfillment_buttons,
		show_noon_upload_order_invoice_dialog,
		run_noon_release_stuck_oos_reservation,
		add_noon_out_of_stock_rows,
		confirm_noon_out_of_stock,
		send_noon_out_of_stock,
		on_noon_row_render,
		on_noon_row_button,
		server_message,
		timing,
	};

	if (typeof frappe !== "undefined") {
		frappe.marketplace_integrations = frappe.marketplace_integrations || {};
		frappe.marketplace_integrations.noon_fulfillment = exported;
		// The row form handlers register once, even if this asset is loaded twice.
		if (frappe.ui && frappe.ui.form && !frappe.ui.form.__mi_noon_row_hooks) {
			frappe.ui.form.__mi_noon_row_hooks = true;
			frappe.ui.form.on("Sales Order Item", {
				form_render: (frm, cdt, cdn) =>
					frappe.marketplace_integrations.noon_fulfillment.on_noon_row_render(frm, cdt, cdn),
				[OOS_BUTTON]: (frm, cdt, cdn) =>
					frappe.marketplace_integrations.noon_fulfillment.on_noon_row_button(frm, cdt, cdn),
			});
		}
	}

	if (typeof module !== "undefined") {
		module.exports = exported;
	}
})();
