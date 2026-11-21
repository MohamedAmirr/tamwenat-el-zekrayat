// Copyright (c) 2026, Tamwenat El Zekrayat and contributors
// For license information, please see license.txt

// Sales Order form extension (#183): after the merchant Closes or Cancels a
// marketplace Sales Order in ERPNext, offer to cancel the order on the platform
// too. The ERPNext action always runs first and is never changed; nothing reaches
// the platform without an explicit confirmation. Platform wording, eligibility and
// cancellation reasons all come from get_platform_cancel_option - nothing here
// names a platform.
//
// Wrapped in a function: every doctype_js file shares one scope with ERPNext's own
// Sales Order script, so top-level names here could collide with its.
(function () {
	const OPTION_METHOD =
		"marketplace_integrations.api.marketplace_outbound.get_platform_cancel_option";
	const REQUEST_METHOD =
		"marketplace_integrations.api.marketplace_outbound.request_platform_cancel";
	const ORDER_EDIT_PREVIEW_METHOD =
		"marketplace_integrations.api.marketplace_outbound.get_order_edit_preview";
	const ORDER_EDIT_METHOD = "marketplace_integrations.api.marketplace_outbound.edit_order";
	const CANCEL_ORDER_METHOD = "marketplace_integrations.api.marketplace_outbound.cancel_order";
	const CANCEL_CHOICE_METHOD = "marketplace_integrations.api.marketplace_outbound.get_shopify_cancel_choice";
	const SKIP_PUSH_METHOD = "marketplace_integrations.api.marketplace_outbound.skip_shopify_cancel_push";
	const MARKETPLACE_TAB_BREAK_FIELD = "custom_marketplace_order_tab";
	const MARKETPLACE_TAB_FIELD = "custom_marketplace_order_html";
	const MARKETPLACE_SUMMARY_METHOD = "marketplace_integrations.api.sales_order_marketplace.get_marketplace_order_summary";
	const MARKETPLACE_REFRESH_METHODS = {
		Talabat: ["marketplace_integrations.api.talabat_orders.refresh_order", "Refresh from Talabat", "Refreshing from Talabat..."],
		Shopify: ["marketplace_integrations.api.shopify_orders.refresh_order", "Refresh from Shopify", "Refreshing from Shopify..."],
		Noon: ["marketplace_integrations.api.noon_orders.refresh_order", "Refresh from Noon", "Queuing Noon refresh..."],
	};
	const NOON_FULFILLMENT_ASSET = "/assets/marketplace_integrations/js/noon_fulfillment.js";
	const MARKETPLACE_SYNC_METHOD = "marketplace_integrations.api.marketplace_orders.sync_to_erpnext";

	function toggle_marketplace_tab(frm, visible) {
		if (!frm.toggle_display) return;
		frm.toggle_display(MARKETPLACE_TAB_BREAK_FIELD, visible);
		frm.toggle_display(MARKETPLACE_TAB_FIELD, visible);
	}

	function marketplace_row(label, value) {
		if (value === null || value === undefined || value === "") return "";
		const escape = frappe.utils.escape_html;
		return `<tr><th>${escape(__(label))}</th><td>${escape(value)}</td></tr>`;
	}

	function marketplace_section(title, rows) {
		const content = rows.filter(Boolean).join("");
		return content ? `<h5>${frappe.utils.escape_html(__(title))}</h5><table class="table table-bordered"><tbody>${content}</tbody></table>` : "";
	}

	// Which amounts each platform stores; zero rows are dropped except the always-shown ones.
	// Shopify amounts are stored in the shop currency (shopMoney). Talabat's payload carries no
	// currency and Noon stores no order amounts, so no currency is claimed for those.
	const MARKETPLACE_AMOUNTS = {
		Shopify: [
			["sub_total", "Subtotal"], ["discount", "Discount"], ["shipping_total", "Shipping Total"],
			["total_taxes", "Total Taxes"], ["duties_total", "Duties Total"], ["order_total", "Order Total", true],
		],
		Talabat: [
			["sub_total", "Subtotal"], ["discount", "Discount"], ["delivery_fee", "Delivery Fee"],
			["service_fee", "Service Fee"], ["container_charge", "Container Charge"],
			["difference_to_minimum", "Difference to Minimum"], ["total_taxes", "Total Taxes"],
			["order_total", "Order Total", true],
			["collect_at_pickup", "Cash Talabat says should be collected at delivery", true],
		],
	};
	const PAYMENT_ENTRY_STATUS = { 0: "Draft", 1: "Submitted", 2: "Cancelled" };

	function marketplace_amount(value, currency) {
		const number = Number(value || 0);
		if (currency && typeof format_currency === "function") return format_currency(number, currency);
		return typeof format_number === "function" ? format_number(number, null, 2) : String(number);
	}

	function marketplace_datetime(value) {
		return value && frappe.datetime && frappe.datetime.str_to_user ? frappe.datetime.str_to_user(value) : value;
	}

	// The same rule as the Marketplace Order form's add_erpnext_sync_action
	// (marketplace_order.js); marketplace_order.test.js asserts the two stay in step.
	function should_offer_erpnext_sync(order) {
		return !(order.erpnext_sales_order && order.erpnext_sync_status === "Synced");
	}

	function marketplace_summary_html(summary) {
		const escape = frappe.utils.escape_html;
		const href = `/app/marketplace-order/${encodeURIComponent(summary.name)}`;
		const identity = marketplace_section("Marketplace Order", [
			marketplace_row("Platform", summary.platform),
			marketplace_row("Order Reference", summary.reference),
			marketplace_row("External Order ID", summary.external_order_id),
			marketplace_row("Order Status", summary.status),
			marketplace_row("Financial Status", summary.financial_status),
			marketplace_row("Fulfillment Status", summary.fulfillment_status),
			marketplace_row("Cancelled At", marketplace_datetime(summary.cancelled_at)),
			marketplace_row("Cancellation Reason", summary.cancellation_reason),
			marketplace_row("Received On", marketplace_datetime(summary.received_on)),
			marketplace_row("Last Platform Update On", marketplace_datetime(summary.last_platform_update_on)),
		]);
		const customer = marketplace_section("Customer and Delivery", [
			marketplace_row("Customer", summary.customer), marketplace_row("Phone", summary.phone),
			marketplace_row("Email", summary.email), marketplace_row("Delivery Address", summary.address),
			marketplace_row("City", summary.city),
		]);
		const amounts = summary.amounts || {};
		const amount_rows = (MARKETPLACE_AMOUNTS[summary.platform] || [])
			.filter(([key, , always]) => always || Number(amounts[key] || 0) !== 0)
			.map(([key, label]) => marketplace_row(label, marketplace_amount(amounts[key], amounts.currency)));
		const payment_rows = [];
		if (summary.platform === "Talabat") payment_rows.push(marketplace_row("Talabat Payment Type", summary.payment_type));
		if (summary.platform === "Shopify") for (const transaction of summary.payment_transactions || []) {
			payment_rows.push(marketplace_row("Transaction", [transaction.kind, transaction.status, transaction.gateway, transaction.amount, transaction.currency, transaction.errorCode].filter(Boolean).join(" · ")));
		}
		if (summary.platform === "Shopify") for (const entry of summary.mirrored_entries || []) {
			payment_rows.push(marketplace_row("Mirrored Payment Entry", [entry.name, __(PAYMENT_ENTRY_STATUS[entry.docstatus] || "Draft"), entry.paid_amount, entry.custom_marketplace_transaction_id].filter(Boolean).join(" · ")));
		}
		if (summary.platform === "Shopify" && summary.latest_payment_log) {
			const log = summary.latest_payment_log;
			payment_rows.push(marketplace_row("Latest Payment Sync", [__(log.status), marketplace_datetime(log.attempted_at), log.message].filter(Boolean).join(" · ")));
		}
		const payment = marketplace_section("Payment", payment_rows);
		const sync = marketplace_section("ERPNext Synchronization", [
			marketplace_row("ERPNext Sync Status", summary.erpnext_sync_status),
			marketplace_row("Sync Action State", summary.sync_action_state ? __(summary.sync_action_state) : ""),
			marketplace_row("Last ERPNext Sync On", marketplace_datetime(summary.last_erpnext_sync_on)),
			marketplace_row("Last Sync Error", summary.last_sync_error),
		]);
		return `${identity}${customer}${marketplace_section("Amounts", amount_rows)}${payment}${sync}<p><a class="btn btn-default" href="${escape(href)}">${escape(__("Open Marketplace Order"))}</a></p>`;
	}

	function add_marketplace_order_tab(frm) {
		const sales_order = frm.doc && frm.doc.name;
		const marketplace_order = frm.doc && frm.doc.custom_marketplace_order;
		if (!marketplace_order || !sales_order) {
			const field = frm.get_field && frm.get_field(MARKETPLACE_TAB_FIELD);
			if (field && field.$wrapper) field.$wrapper.html("");
			toggle_marketplace_tab(frm, false);
			return Promise.resolve();
		}
		toggle_marketplace_tab(frm, true);
		const field = frm.get_field && frm.get_field(MARKETPLACE_TAB_FIELD);
		if (!field || !field.$wrapper) return Promise.resolve();
		const request = (frm.__mi_marketplace_request = (frm.__mi_marketplace_request || 0) + 1);
		return frappe.call({ method: MARKETPLACE_SUMMARY_METHOD, type: "POST", args: { sales_order }, silent: true }).then(
			(r) => {
				if (request !== frm.__mi_marketplace_request || frm.doc.name !== sales_order) return;
				const summary = r.message;
				if (!summary) {
					field.$wrapper.html(`<p class="text-muted">${frappe.utils.escape_html(__("Marketplace Order summary unavailable."))}</p>`);
					return;
				}
				field.$wrapper.html(marketplace_summary_html(summary));
				const refresh = MARKETPLACE_REFRESH_METHODS[summary.platform];
				if (refresh) {
					field.$wrapper.append(`<p><button class="btn btn-default" data-marketplace-action="refresh">${frappe.utils.escape_html(__(refresh[1]))}</button></p>`);
				}
				if (should_offer_erpnext_sync(summary)) {
					field.$wrapper.append(`<p><button class="btn btn-primary" data-marketplace-action="sync">${frappe.utils.escape_html(__("Retry Sync"))}</button></p>`);
				}
				field.$wrapper.off("click.mi_marketplace").on("click.mi_marketplace", "[data-marketplace-action]", (event) => {
					if (event.currentTarget.getAttribute("data-marketplace-action") === "refresh") {
						const [method, _label, freeze_message] = refresh;
						frappe.call({ method, args: { marketplace_order: summary.name }, freeze: true, freeze_message: __(freeze_message) }).then((response) => {
							if (summary.platform === "Noon") {
								const queued = (response.message || {}).queued;
								frappe.show_alert({ message: queued ? __("Noon order refresh queued. Refresh this document to see the results.") : __("A Noon refresh for this order is already queued or running."), indicator: "blue" });
							} else frm.reload_doc();
						});
					} else {
						frappe.call({ method: MARKETPLACE_SYNC_METHOD, args: { marketplace_order: summary.name }, freeze: true, freeze_message: __("Synchronizing with ERPNext...") }).then((response) => {
							if (!response.message.ok) frappe.msgprint({ message: frappe.utils.escape_html(response.message.error), indicator: "red", title: __("ERPNext Sync Blocked") });
							else if (response.message.skipped) frappe.show_alert({ message: frappe.utils.escape_html(response.message.message || ""), indicator: "blue" });
							else if (response.message.executed_action) frappe.show_alert({ message: frappe.utils.escape_html(response.message.executed_message || ""), indicator: "green" });
							frm.reload_doc();
						});
					}
				});
			},
			() => {
				if (request === frm.__mi_marketplace_request && frm.doc.name === sales_order) {
					field.$wrapper.html(`<p class="text-muted">${frappe.utils.escape_html(__("Marketplace Order summary unavailable."))}</p>`);
				}
			}
		);
	}

	// Only submitted (1) or cancelled (2) Sales Orders linked to a Marketplace Order.
	function is_marketplace_sales_order(doc) {
		return Boolean(doc && doc.custom_marketplace_order && doc.docstatus >= 1);
	}

	function is_closed_or_cancelled(doc) {
		return Boolean(doc) && (doc.status === "Closed" || doc.status === "Cancelled");
	}

	// Already requested, already cancelled on the platform, or delivered/billed:
	// there is nothing to ask the merchant, so no dialog at all.
	function needs_no_dialog(option) {
		return Boolean(
			option.already_requested || option.platform_cancelled || option.erp_executed
		);
	}

	function can_offer_cancel(option) {
		return Boolean(option) && option.state === "eligible" && !needs_no_dialog(option);
	}

	// ERPNext has no hook for Close, so the click is remembered on the form and acted
	// on at the next refresh that shows Closed. The flag holds the name of the Sales
	// Order that was Closed. Called on every refresh: returns true only when the refresh
	// is of that same document and shows Closed, and clears the flag either way, so
	// opening or reloading an already Closed order (or another one) never prompts. A
	// failed Close leaves the flag set until the next refresh.
	function consume_close_request(frm) {
		const requested = frm.__mi_close_requested;
		frm.__mi_close_requested = null;
		return Boolean(requested) && requested === frm.doc.name && frm.doc.status === "Closed";
	}

	// Wrap frm.cscript.close_sales_order once per form (an own-property wrapper; the
	// original stays on ERPNext's controller). Returns false when ERPNext no longer has
	// the method, so the caller can report it instead of silently losing the Close prompt.
	function wrap_close_sales_order(frm) {
		if (frm.__mi_close_wrapped) return true;
		const original = frm.cscript && frm.cscript.close_sales_order;
		if (typeof original !== "function") return false;
		frm.cscript.close_sales_order = function (...args) {
			frm.__mi_close_requested = frm.doc.name;
			return original.apply(this, args);
		};
		frm.__mi_close_wrapped = true;
		return true;
	}

	function reason_field(option) {
		const reasons = option.cancellation_reasons || [];
		if (!reasons.length) return null;
		return {
			fieldname: "cancellation_reason",
			fieldtype: "Select",
			label: __("Cancellation Reason"),
			options: reasons.join("\n"),
			reqd: 1,
		};
	}

	// instruction and reasons can contain external platform text, so they are escaped.
	function blocked_message(option) {
		const escape = frappe.utils.escape_html;
		const reasons = (option.reasons || [])
			.map((reason) => `<li>${escape(reason)}</li>`)
			.join("");
		return (
			(option.instruction ? `<p>${escape(option.instruction)}</p>` : "") +
			(reasons ? `<ul>${reasons}</ul>` : "")
		);
	}

	function result_alert(result) {
		if (result.already_requested) {
			return __("Cancellation was already requested.");
		}
		if (result.refreshed === false) {
			return __(
				"Cancellation requested. The Marketplace Order will update on the next webhook or refresh."
			);
		}
		return __("Cancellation requested.");
	}

	function request_cancel(frm, values) {
		return (
			frappe
				.call({
					method: REQUEST_METHOD,
					type: "POST",
					args: {
						sales_order: frm.doc.name,
						cancellation_reason: (values && values.cancellation_reason) || "",
					},
					freeze: true,
					freeze_message: __("Cancelling on the platform..."),
				})
				.then((r) => {
					frappe.show_alert({
						message: result_alert(r.message || {}),
						indicator: "green",
					});
					return frm.reload_doc();
				})
				// A server error already showed Frappe's standard message; the ERPNext
				// Close/Cancel that came before is untouched.
				.catch(() => {})
		);
	}

	function show_cancel_dialog(frm, option) {
		const platform = option.platform;
		const escaped_platform = frappe.utils.escape_html(platform);
		const fields = [
			{
				fieldtype: "HTML",
				fieldname: "warning",
				options: `<p>${frappe.utils.escape_html(
					__("Also cancel this order on {0}? This cannot be undone.", [platform])
				)}</p>`,
			},
		];
		const reason = reason_field(option);
		if (reason) fields.push(reason);
		const dialog = new frappe.ui.Dialog({
			title: __("Cancel on {0}", [escaped_platform]),
			fields,
			primary_action_label: __("Cancel on {0}", [escaped_platform]),
			primary_action(values) {
				dialog.hide();
				request_cancel(frm, values);
			},
			secondary_action_label: __("Not now"),
			secondary_action() {
				dialog.hide();
			},
		});
		dialog.show();
	}

	function show_blocked_dialog(option) {
		frappe.msgprint({
			title: __("Cancel on {0}", [frappe.utils.escape_html(option.platform)]),
			message: blocked_message(option),
			indicator: "orange",
		});
	}

	// Shopify accepted the edit; only the local refresh can be missing.  That is a warning,
	// not a success: the Marketplace Order still shows the old lines until the next refresh.
	function shopify_edit_alert(result) {
		if (result.refreshed === false) {
			return {
				message: __(
					"Item changes were sent to Shopify, but the order could not be refreshed. Use Refresh from Shopify on the order."
				),
				indicator: "orange",
			};
		}
		return { message: __("Item changes sent to Shopify."), indicator: "green" };
	}

	// Edit Order (#500) REPLACES ERPNext's "Update Items" on a submitted Shopify Sales Order: an item edit has
	// to reach Shopify first. The server is the real gate (stage lock, stock, Shopify checks); this only
	// offers the action and hides the button that would bypass it.
	function is_shopify_sales_order(doc) {
		return Boolean(
			doc && doc.custom_marketplace_order && doc.docstatus === 1 && doc.custom_marketplace_platform === "Shopify"
		);
	}

	// ERPNext adds "Update Items" in two places (its form script and its controller) and the order they run
	// in against this file is not ours to rely on, so the button is dropped when it is added (an own-property
	// wrapper on the form instance, like wrap_close_sales_order) and removed again after each refresh.
	function suppress_update_items(frm) {
		if (!frm || typeof frm.add_custom_button !== "function") return;
		if (!frm.__mi_update_items_wrapped) {
			const original = frm.add_custom_button;
			frm.add_custom_button = function (label, ...args) {
				if (label === __("Update Items") && is_shopify_sales_order(frm.doc)) return;
				return original.call(this, label, ...args);
			};
			frm.__mi_update_items_wrapped = true;
		}
		if (is_shopify_sales_order(frm.doc) && typeof frm.remove_custom_button === "function") {
			frm.remove_custom_button(__("Update Items"));
		}
	}

	function can_offer_edit_order(frm) {
		const doc = frm.doc;
		if (!is_shopify_sales_order(doc) || is_closed_or_cancelled(doc)) return false;
		return Boolean(frappe.model.can_write("Sales Order") && frappe.model.can_submit("Sales Order"));
	}

	function show_edit_order_blocked(preview) {
		const reasons = (preview.reasons || [])
			.map((reason) => `<li>${frappe.utils.escape_html(reason)}</li>`)
			.join("");
		frappe.msgprint({
			title: __("This order cannot be edited"),
			indicator: "orange",
			message: `<ul>${reasons}</ul>`,
		});
	}

	function edit_order_alert(result) {
		if (result.applied === false) {
			return {
				message: __(
					"Shopify accepted the change, but ERPNext could not be updated: {0}. ERPNext follows Shopify automatically; use Retry Sync on the Marketplace tab to do it now.",
					[frappe.utils.escape_html(result.apply_error || "")]
				),
				indicator: "orange",
			};
		}
		return shopify_edit_alert(result);
	}

	// Plain warnings from the server (for example: the carrier shipment is started and is not updated).
	function warnings_html(preview) {
		return (preview.warnings || [])
			.map((warning) => `<p class="text-warning">${frappe.utils.escape_html(warning)}</p>`)
			.join("");
	}

	function show_edit_order_confirm(frm, preview, items) {
		const rows = (preview.changes || [])
			.map((change) => {
				const quantity = change.quantity === 0 ? __("Remove") : change.quantity;
				return `<li>${frappe.utils.escape_html(change.label || "")} — ${frappe.utils.escape_html(
					String(change.old_quantity)
				)} → ${frappe.utils.escape_html(String(quantity))}</li>`;
			})
			.join("");
		const dialog = new frappe.ui.Dialog({
			title: __("Send the changes to Shopify"),
			fields: [
				{
					fieldtype: "HTML",
					fieldname: "preview",
					options: `<p>${frappe.utils.escape_html(
						__("Shopify is updated first, then this order. The customer will not be notified.")
					)}</p>${warnings_html(preview)}<ul>${rows}</ul><p>${frappe.utils.escape_html(
						__("Items and shipping total, before tax: {0} → {1}", [
							format_currency(preview.old_total, frm.doc.currency),
							format_currency(preview.new_total, frm.doc.currency),
						])
					)}</p>`,
				},
			],
			primary_action_label: __("Send to Shopify"),
			primary_action() {
				dialog.hide();
				frappe
					.call({
						method: ORDER_EDIT_METHOD,
						type: "POST",
						args: { sales_order: frm.doc.name, items, fingerprint: preview.fingerprint },
						freeze: true,
						freeze_message: __("Sending the changes to Shopify..."),
					})
					.then((r) => {
						frappe.show_alert(edit_order_alert(r.message || {}));
						return frm.reload_doc();
					})
					.catch(() => {});
			},
			secondary_action_label: __("Not now"),
			secondary_action() {
				dialog.hide();
			},
		});
		dialog.show();
	}

	function review_edit_order(frm, items) {
		return frappe
			.call({
				method: ORDER_EDIT_PREVIEW_METHOD,
				type: "POST",
				args: { sales_order: frm.doc.name, items },
				freeze: true,
				freeze_message: __("Checking the changes..."),
			})
			.then((r) => {
				const preview = r.message;
				if (!preview) return;
				if (preview.state === "eligible" && (preview.changes || []).length) {
					show_edit_order_confirm(frm, preview, items);
				} else {
					show_edit_order_blocked(preview);
				}
			})
			.catch(() => {});
	}

	function show_edit_order_dialog(frm, preview) {
		const rows = (preview.rows || []).map((row) => ({ ...row }));
		const dialog = new frappe.ui.Dialog({
			title: __("Edit Order"),
			size: "large",
			fields: [
				{
					fieldtype: "HTML",
					fieldname: "intro",
					options: `<p>${frappe.utils.escape_html(
						__(
							"Change an item quantity, set it to 0 to remove the item, or add a row for a new item. The change goes to Shopify first."
						)
					)}</p>${warnings_html(preview)}`,
				},
				{
					fieldtype: "Table",
					fieldname: "items",
					// Existing rows are removed with quantity 0 (the server ignores the item of an existing row).
					cannot_delete_rows: true,
					in_place_edit: true,
					data: rows,
					get_data: () => rows,
					fields: [
						{ fieldtype: "Data", fieldname: "docname", hidden: 1 },
						{ fieldtype: "Link", fieldname: "item_code", label: __("Item Code"), options: "Item", in_list_view: 1, columns: 3 },
						{ fieldtype: "Data", fieldname: "item_name", label: __("Item Name"), read_only: 1, in_list_view: 1, columns: 4 },
						{ fieldtype: "Float", fieldname: "qty", label: __("Quantity"), reqd: 1, in_list_view: 1, columns: 2 },
					],
				},
			],
			primary_action_label: __("Review"),
			primary_action(values) {
				const items = (values.items || [])
					.filter((row) => row.docname || row.item_code)
					.map((row) => ({ docname: row.docname || "", item_code: row.item_code, qty: row.qty }));
				dialog.hide();
				review_edit_order(frm, items);
			},
		});
		dialog.show();
	}

	// First call: the rows, or why nothing can be edited (picked up, shipment started, Shopify refuses).
	function open_edit_order(frm) {
		const name = frm.doc.name;
		return frappe
			.call({
				method: ORDER_EDIT_PREVIEW_METHOD,
				type: "POST",
				args: { sales_order: name },
				freeze: true,
				freeze_message: __("Checking the order..."),
			})
			.then((r) => {
				const preview = r.message;
				if (!preview || frm.doc.name !== name || preview.state === "not_applicable") return;
				if (preview.state === "blocked") show_edit_order_blocked(preview);
				else show_edit_order_dialog(frm, preview);
			})
			.catch(() => {});
	}

	function add_edit_order_button(frm) {
		if (!can_offer_edit_order(frm)) return;
		frm.add_custom_button(__("Edit Order"), () => open_edit_order(frm));
	}

	// Cancel Order (#501): the whole chain before pickup - carrier shipment, draft Delivery Note, Sales Order, then
	// Shopify - stopping at the first failure. The server decides; this offers it and reports the result.
	function can_offer_cancel_order(frm) {
		const doc = frm.doc;
		if (!is_shopify_sales_order(doc) || is_closed_or_cancelled(doc)) return false;
		return Boolean(
			frappe.model.can_cancel("Sales Order") &&
				frappe.model.can_read("Marketplace Order") &&
				frappe.model.can_write("Marketplace Order")
		);
	}

	// The result of the chain: what was done and, when it stopped, where and why (the reason is external text).
	function cancel_order_result_message(result) {
		const escape = frappe.utils.escape_html;
		const stopped = result.stopped || {};
		const done = (result.completed_labels || []).length
			? `<p>${escape(__("Done:"))} ${escape((result.completed_labels || []).join(", "))}</p>`
			: "";
		return (
			`<p>${escape(__("The cancellation stopped at: {0}", [stopped.label || ""]))}</p>` +
			`<p>${escape(stopped.message || "")}</p>` +
			done +
			`<p>${escape(__("Everything before this step stays done. Press Cancel Order again to continue from this step."))}</p>`
		);
	}

	function run_cancel_order(frm, values) {
		return frappe
			.call({
				method: CANCEL_ORDER_METHOD,
				type: "POST",
				args: { sales_order: frm.doc.name, cancellation_reason: (values && values.cancellation_reason) || "" },
				freeze: true,
				freeze_message: __("Cancelling the order..."),
			})
			.then((r) => {
				const result = r.message || {};
				if (result.ok) {
					frappe.show_alert({ message: __("The order was cancelled."), indicator: "green" });
				} else {
					frappe.msgprint({
						title: __("The order was not fully cancelled"),
						indicator: "orange",
						message: cancel_order_result_message(result),
					});
				}
				return frm.reload_doc();
			})
			.catch(() => {});
	}

	function show_cancel_order_dialog(frm, option) {
		const fields = [
			{
				fieldtype: "HTML",
				fieldname: "warning",
				options: `<p>${frappe.utils.escape_html(
					__(
						"The shipment at the carrier is cancelled, the draft Delivery Note is deleted, the Sales Order is cancelled and the order is cancelled on {0}. This cannot be undone.",
						[option.platform]
					)
				)}</p>`,
			},
		];
		const reason = reason_field(option);
		if (reason) fields.push(reason);
		const dialog = new frappe.ui.Dialog({
			title: __("Cancel Order"),
			fields,
			primary_action_label: __("Cancel Order"),
			primary_action(values) {
				dialog.hide();
				run_cancel_order(frm, values);
			},
			secondary_action_label: __("Not now"),
			secondary_action() {
				dialog.hide();
			},
		});
		dialog.show();
	}

	function open_cancel_order(frm) {
		const name = frm.doc.name;
		return frappe
			.call({ method: OPTION_METHOD, type: "POST", args: { sales_order: name } })
			.then((r) => {
				const option = r.message;
				if (!option || frm.doc.name !== name || option.state === "not_applicable") return;
				if (option.state === "blocked" && !needs_no_dialog(option)) show_blocked_dialog(option);
				else show_cancel_order_dialog(frm, option);
			})
			.catch(() => {});
	}

	function add_cancel_order_button(frm) {
		if (!can_offer_cancel_order(frm)) return;
		frm.add_custom_button(__("Cancel Order"), () => open_cancel_order(frm));
	}

	// Create Shipment (one-step): the whole order's draft Delivery Note AND its Bosta shipment in one click
	// (api/order_shipment.py, the same service as the list's bulk action). The server decides everything (guards,
	// stock, permissions); this offers the button, then watches the note's Integration Status until Bosta answers.
	// The partial or reviewed shipment stays Create > Delivery Note.
	const CREATE_SHIPMENT_METHOD = "marketplace_integrations.api.order_shipment.create_shipment";
	const SHIPMENT_STATE_METHOD = "marketplace_integrations.api.order_shipment.shipment_state";
	// Exposed so a test can shorten the wait.
	const SHIPMENT_TIMING = { poll_ms: 2000, limit: 30 };
	// Mirrors BOSTA_EXCLUDED_PLATFORMS in integrations/bosta/constants.py (a test keeps the two equal).
	const BOSTA_EXCLUDED_PLATFORMS = ["Noon", "Talabat"];
	const SHIPMENT_OPEN_STATUSES = ["To Deliver", "To Deliver and Bill"];

	function can_offer_create_shipment(frm) {
		const doc = frm.doc;
		if (!doc || doc.docstatus !== 1 || !doc.custom_marketplace_order || !doc.custom_marketplace_platform) return false;
		if (BOSTA_EXCLUDED_PLATFORMS.includes(doc.custom_marketplace_platform)) return false;
		if (!SHIPMENT_OPEN_STATUSES.includes(doc.status)) return false;
		return Boolean(frappe.model.can_create("Delivery Note"));
	}

	function open_delivery_note(name) {
		if (frappe.set_route) frappe.set_route("Form", "Delivery Note", name);
	}

	function note_link(name) {
		const escape = frappe.utils.escape_html;
		return `<a href="/app/delivery-note/${encodeURIComponent(name)}">${escape(name)}</a>`;
	}

	// One poll of the note until the Bosta create finished (accepted), failed (the note is kept, Unknown) or was
	// refused for good (the server deleted the draft note: nothing to open).
	// `wait.closed` is set when the dialog is closed (its X or "Close, keep sending"): the poll then stops quietly, with
	// no later alert, message or jump to the note.
	function watch_shipment(frm, note, dialog, attempt, wait) {
		wait = wait || { closed: false };
		return frappe
			.call({ method: SHIPMENT_STATE_METHOD, args: { delivery_note: note } })
			.then((r) => {
				if (wait.closed) return null;
				const values = (r && r.message) || {};
				const state = values.state || "";
				if (state === "accepted") {
					dialog.hide();
					frappe.show_alert({ message: __("The Bosta shipment was created."), indicator: "green" });
					return open_delivery_note(note);
				}
				if (state === "discarded" || state === "missing") {
					dialog.hide();
					frappe.msgprint({
						title: __("Bosta did not accept the shipment"),
						indicator: "red",
						message:
							`<p>${frappe.utils.escape_html(
								state === "discarded"
									? __("Bosta did not accept the shipment: {0}. The Delivery Note {1} was deleted.", [values.reason || "", note])
									: __("The Delivery Note {0} no longer exists.", [note])
							)}</p>`,
					});
					return null;
				}
				if (state === "failed") {
					dialog.hide();
					frappe.msgprint({
						title: __("Bosta did not accept the shipment"),
						indicator: "red",
						message:
							`<p>${frappe.utils.escape_html(values.reason || __(values.status || ""))}</p>` +
							`<p>${note_link(note)}</p>`,
					});
					return open_delivery_note(note);
				}
				if (attempt >= SHIPMENT_TIMING.limit) {
					dialog.hide();
					frappe.msgprint({
						title: __("Still creating at Bosta"),
						indicator: "orange",
						message: `<p>${frappe.utils.escape_html(__("Bosta has not answered yet. Open the Delivery Note to see the result."))}</p><p>${note_link(note)}</p>`,
					});
					return open_delivery_note(note);
				}
				return new Promise((resolve) => setTimeout(resolve, SHIPMENT_TIMING.poll_ms)).then(() =>
					wait.closed ? null : watch_shipment(frm, note, dialog, attempt + 1, wait)
				);
			})
			.catch(() => dialog.hide());
	}

	function show_shipment_wait(frm, note) {
		const wait = { closed: false };
		const dialog = new frappe.ui.Dialog({
			title: __("Creating the Bosta shipment"),
			onhide() {
				wait.closed = true;
			},
			fields: [
				{
					fieldtype: "HTML",
					fieldname: "body",
					options: `<p>${frappe.utils.escape_html(__("The Delivery Note {0} was created. Waiting for Bosta...", [note]))}</p>`,
				},
			],
			secondary_action_label: __("Close, keep sending"),
			secondary_action() {
				dialog.hide();
				open_delivery_note(note);
			},
		});
		dialog.show();
		return watch_shipment(frm, note, dialog, 0, wait);
	}

	function run_create_shipment(frm) {
		return frappe
			.call({
				method: CREATE_SHIPMENT_METHOD,
				type: "POST",
				args: { sales_order: frm.doc.name },
				freeze: true,
				freeze_message: __("Creating the Delivery Note..."),
			})
			.then((r) => {
				const result = (r && r.message) || {};
				const message = frappe.utils.escape_html(result.message || "");
				if (result.status === "sent") return show_shipment_wait(frm, result.delivery_note);
				if (result.status === "failed" && result.delivery_note) {
					// Only a REUSED draft (the person's own note) is kept with a failed row; a note this action made is
					// deleted on a definite failure and the row has no note (the red message below).
					frappe.msgprint({
						title: __("The Delivery Note needs your attention"),
						indicator: "orange",
						message: `<p>${message}</p><p>${note_link(result.delivery_note)}</p>`,
					});
					return open_delivery_note(result.delivery_note);
				}
				// Refused (nothing was created, or the note that is in the way is linked) or failed with no note.
				frappe.msgprint({
					title: __("The shipment was not created"),
					indicator: "red",
					message: `<p>${message}</p>` + (result.delivery_note ? `<p>${note_link(result.delivery_note)}</p>` : ""),
				});
				return null;
			})
			.catch(() => {});
	}

	function add_create_shipment_button(frm) {
		if (!can_offer_create_shipment(frm)) return;
		frm.add_custom_button(__("Create Shipment"), () => run_create_shipment(frm));
	}

	function add_cancel_button(frm, option) {
		frm.add_custom_button(
			__("Cancel on {0}", [frappe.utils.escape_html(option.platform)]),
			() => show_cancel_dialog(frm, option)
		);
	}

	// One option fetch. `prompt` is true right after a Close/Cancel; `button` says
	// whether the fetch may add the "Cancel on <platform>" button (after_cancel does
	// not: the refresh that follows it clears custom buttons and adds its own).
	function load_platform_option(frm, { prompt, button }) {
		// Only System Manager can read Marketplace Order, and the option endpoint checks
		// read on it. For anyone else that check fails server-side and Frappe itself shows
		// its "Not permitted" popup and re-routes, which a .catch cannot suppress, so the
		// call is not made at all.
		if (!frappe.model.can_read("Marketplace Order")) return Promise.resolve();
		const sales_order = frm.doc.name;
		return (
			frappe
				.call({ method: OPTION_METHOD, type: "POST", args: { sales_order } })
				.then((r) => {
					const option = r.message;
					// The form may have moved on to another document while the call ran.
					if (
						!option ||
						option.state === "not_applicable" ||
						frm.doc.name !== sales_order
					)
						return;
					if (button && can_offer_cancel(option) && is_closed_or_cancelled(frm.doc)) {
						add_cancel_button(frm, option);
					}
					if (!prompt || needs_no_dialog(option)) return;
					if (option.state === "eligible") show_cancel_dialog(frm, option);
					else if (option.state === "blocked") show_blocked_dialog(option);
				})
				// A failed fetch only loses the prompt/button; Frappe has already shown its
				// own error for a server failure, and the ERPNext action is unaffected.
				.catch(() => {})
		);
	}

	function noon_fulfillment_module() {
		return frappe.marketplace_integrations && frappe.marketplace_integrations.noon_fulfillment;
	}

	// Noon fulfillment lives on the Sales Order form (the buttons and dialogs are in
	// public/js/noon_fulfillment.js, loaded on demand). Only a submitted Sales Order linked to a
	// Noon Marketplace Order gets it; every endpoint it calls still checks permission on the
	// Marketplace Order.
	function load_noon_fulfillment(frm) {
		const doc = frm.doc;
		if (
			!doc ||
			doc.docstatus !== 1 ||
			!doc.custom_marketplace_order ||
			doc.custom_marketplace_platform !== "Noon"
		)
			return Promise.resolve(null);
		// Marketplace Order read is checked inside add_noon_fulfillment_actions: the item-row Mark as
		// Out of Stock needs only write on the Sales Order.
		const load = frappe.require ? frappe.require(NOON_FULFILLMENT_ASSET).catch(() => {}) : Promise.resolve();
		return load.then(() => {
			const noon = noon_fulfillment_module();
			return noon ? noon.add_noon_fulfillment_actions(frm) : null;
		});
	}

	// #368: Noon has no order-level update, so the server refuses item, quantity and rate edits on a
	// submitted Noon Sales Order (hooks.py, services/noon_item_edit_guard.py). This only tells the
	// user why and where the supported actions are; the server check is the real gate.
	function add_noon_item_edit_hint(frm) {
		const doc = frm.doc;
		if (!doc || doc.docstatus !== 1 || doc.custom_marketplace_platform !== "Noon") return;
		if (typeof frm.set_intro !== "function") return;
		frm.set_intro(
			__(
				"Items and quantities of a Noon order cannot be changed here. Use Mark as Out of Stock on an item row of this Sales Order, or cancel the Delivery Note, which stops at Awaiting Return until a Noon User or System Manager enters the returned quantities in Confirm Returned, and then amend it."
			),
			"orange"
		);
	}

	// Frappe's "Cancel All Documents" (form.js savecancel) cancels the submitted linked documents natively BEFORE
	// before_cancel runs and skips every doctype in frm.ignore_doctypes_on_cancel_all. ERPNext's sales_order.js
	// REASSIGNS that list in onload, so a one-time push would be lost: the property is redefined so every assignment
	// keeps these doctypes in it. The server cascade (order_cancel_cascade) then owns them: it cancels the invoices
	// and notes in order and blocks on payments and returns, which the native dialog would not.
	const CASCADE_OWNED_DOCTYPES = ["Delivery Note", "Sales Invoice", "Payment Entry"];

	function own_cancel_all_documents(frm) {
		if (!frm || frm.__mi_cancel_all_owned) return;
		let stored = frm.ignore_doctypes_on_cancel_all || [];
		Object.defineProperty(frm, "ignore_doctypes_on_cancel_all", {
			configurable: true,
			enumerable: true,
			get: () => Array.from(new Set([...stored, ...CASCADE_OWNED_DOCTYPES])),
			set: (value) => {
				stored = value || [];
			},
		});
		frm.__mi_cancel_all_owned = true;
	}

	// Needs Attention (#533, services/order_attention.py): a red header indicator, a red banner with the reason,
	// and Mark as Resolved. An unflagged order gets nothing, so it looks exactly as before.
	const MARK_RESOLVED_METHOD = "marketplace_integrations.api.order_attention.mark_resolved";

	function is_flagged(doc) {
		return Boolean(doc) && Number(doc.custom_needs_attention) === 1;
	}

	function attention_banner_html(reason) {
		const escape = frappe.utils.escape_html;
		return String(reason || "")
			.split("\n")
			.map((line) => escape(line))
			.join("<br>");
	}

	function run_mark_resolved(frm) {
		return frappe
			.call({
				method: MARK_RESOLVED_METHOD,
				type: "POST",
				args: { sales_order: frm.doc.name },
				freeze: true,
				freeze_message: __("Marking as resolved..."),
			})
			.then(() => {
				frappe.show_alert({ message: __("The order was marked as resolved."), indicator: "green" });
				return frm.reload_doc();
			})
			// A server error already showed Frappe's standard message.
			.catch(() => {});
	}

	function show_needs_attention(frm) {
		if (!is_flagged(frm.doc)) return;
		// The header indicator only on a submitted order; a cancelled one keeps its own, but still shows the banner.
		if (frm.doc.docstatus === 1 && frm.page && typeof frm.page.set_indicator === "function") {
			frm.page.set_indicator(__("Needs Attention"), "red");
		}
		const reason = frm.doc.custom_attention_reason || __("Something about this order needs a person.");
		if (frm.dashboard && typeof frm.dashboard.set_headline_alert === "function") {
			frm.dashboard.set_headline_alert(attention_banner_html(reason), "red");
		}
		if ([1, 2].includes(frm.doc.docstatus) && frappe.model.can_write("Sales Order")) {
			frm.add_custom_button(__("Mark as Resolved"), () => run_mark_resolved(frm));
		}
	}

	function on_setup(frm) {
		suppress_update_items(frm);
		own_cancel_all_documents(frm);
	}

	function on_refresh(frm) {
		suppress_update_items(frm);
		own_cancel_all_documents(frm);
		show_needs_attention(frm);
		load_noon_fulfillment(frm);
		add_marketplace_order_tab(frm);
		if (!wrap_close_sales_order(frm)) {
			console.error(
				"ERPNext no longer defines close_sales_order on the Sales Order form (#183)."
			);
		}
		const closed_by_click = consume_close_request(frm);
		if (!is_marketplace_sales_order(frm.doc)) return;
		add_noon_item_edit_hint(frm);
		add_edit_order_button(frm);
		add_cancel_order_button(frm);
		add_create_shipment_button(frm);
		if (closed_by_click || is_closed_or_cancelled(frm.doc)) {
			load_platform_option(frm, { prompt: closed_by_click, button: true });
		}
	}

	// Native Cancel, after Frappe's own confirmation (form.js runs before_cancel, waits for its promise and cancels
	// only while frappe.validated is true). A submitted Shopify order that Shopify would still cancel asks whether
	// to cancel it on Shopify too: a cancel-and-amend must not cancel the real order. The choice "ERPNext only" is
	// remembered by the server for this user's next cancel of this order. The cascade (invoices, notes, shipments)
	// is the same either way. List-view bulk cancel does not run this form hook and keeps the default (push).
	function before_cancel(frm) {
		// Whether to ask is the server's call (get_shopify_cancel_choice checks the platform and eligibility).
		if (frm.doc.docstatus !== 1 || !frm.doc.custom_marketplace_order) {
			return;
		}
		const order = frm.doc.name;
		return new Promise((resolve) => {
			const proceed = () => resolve();
			const stop = () => {
				frappe.validated = false;
				resolve();
			};
			frappe
				.call({ method: CANCEL_CHOICE_METHOD, type: "POST", args: { sales_order: order } })
				.then((r) => {
					if (!(r.message && r.message.ask)) return proceed();
					let settled = false;
					const settle = (action) => {
						if (settled) return;
						settled = true;
						dialog.hide();
						action();
					};
					const dialog = new frappe.ui.Dialog({
						title: __("Cancel on {0}", ["Shopify"]),
						fields: [
							{
								fieldtype: "HTML",
								fieldname: "choice",
								options: `<p>${frappe.utils.escape_html(
									__(
										"Also cancel this order on Shopify? Choose Cancel in ERPNext only when you will amend the Sales Order: the Shopify order stays open."
									)
								)}</p>`,
							},
						],
						primary_action_label: __("Cancel and push to Shopify"),
						primary_action() {
							settle(proceed);
						},
						secondary_action_label: __("Cancel in ERPNext only"),
						secondary_action() {
							settle(() =>
								frappe
									.call({ method: SKIP_PUSH_METHOD, type: "POST", args: { sales_order: order } })
									.then(proceed)
									.catch(stop)
							);
						},
						// Closing the dialog is the same as Don't cancel.
						onhide() {
							if (!settled) {
								settled = true;
								stop();
							}
						},
					});
					dialog.add_custom_action(__("Don't cancel"), () => settle(stop));
					dialog.show();
				})
				// The question could not be asked: cancel as before (push), Frappe has shown its own error.
				.catch(proceed);
		});
	}

	function on_after_cancel(frm) {
		if (!is_marketplace_sales_order(frm.doc)) return;
		// A Shopify order is cancelled on Shopify by the server after the cancel (and warned about there): no prompt.
		// The "Cancel on Shopify" button still shows on the cancelled order, to retry a failed push.
		if (frm.doc.custom_marketplace_platform === "Shopify") return;
		load_platform_option(frm, { prompt: true, button: false });
	}

	// The flag keeps the form handlers from being registered twice if the file is loaded twice.
	if (typeof frappe !== "undefined") {
		frappe.marketplace_integrations = frappe.marketplace_integrations || {};
		if (frappe.ui && frappe.ui.form && !frappe.ui.form.__mi_sales_order_hooks) {
			frappe.ui.form.__mi_sales_order_hooks = true;
			frappe.ui.form.on("Sales Order", {
				setup: on_setup,
				refresh: on_refresh,
				before_cancel,
				after_cancel: on_after_cancel,
			});
		}
	}

	if (typeof module !== "undefined") {
		module.exports = {
			add_marketplace_order_tab,
			attention_banner_html,
			run_mark_resolved,
			show_needs_attention,
			add_noon_item_edit_hint,
			load_noon_fulfillment,
			add_edit_order_button,
			add_cancel_order_button,
			add_create_shipment_button,
			can_offer_create_shipment,
			run_create_shipment,
			SHIPMENT_TIMING,
			can_offer_cancel_order,
			cancel_order_result_message,
			can_offer_edit_order,
			edit_order_alert,
			is_shopify_sales_order,
			open_edit_order,
			suppress_update_items,
			shopify_edit_alert,
			blocked_message,
			can_offer_cancel,
			consume_close_request,
			is_closed_or_cancelled,
			is_marketplace_sales_order,
			load_platform_option,
			needs_no_dialog,
			marketplace_summary_html,
			should_offer_erpnext_sync,
			on_after_cancel,
			before_cancel,
			own_cancel_all_documents,
			on_refresh,
			reason_field,
			result_alert,
			show_cancel_dialog,
			wrap_close_sales_order,
		};
	}
})();
