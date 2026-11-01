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
	const SHOPIFY_EDIT_PREVIEW_METHOD =
		"marketplace_integrations.api.marketplace_outbound.get_shopify_order_edit_preview";
	const SHOPIFY_EDIT_SEND_METHOD =
		"marketplace_integrations.api.marketplace_outbound.send_shopify_order_edit";
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
				// The Noon shipments summary comes from the same call that drives the Noon
				// Fulfillment buttons (load_noon_fulfillment), so wait for it before rendering.
				const noon_state = summary.platform === "Noon" ? frm.__mi_noon_state || Promise.resolve(null) : Promise.resolve(null);
				return noon_state.then((state) => {
				if (request !== frm.__mi_marketplace_request || frm.doc.name !== sales_order) return;
				const noon = noon_fulfillment_module();
				let html = marketplace_summary_html(summary);
				if (state && noon) html += noon.noon_shipments_html(state.shipments);
				field.$wrapper.html(html);
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

	function show_shopify_edit_dialog(frm, preview) {
		const rows = (preview.changes || [])
			.map((change) => {
				const quantity = change.quantity === 0 ? __("Remove") : change.quantity;
				return `<li>${frappe.utils.escape_html(change.label || "")} — ${frappe.utils.escape_html(
					String(change.old_quantity)
				)} → ${frappe.utils.escape_html(String(quantity))}</li>`;
			})
			.join("");
		const dialog = new frappe.ui.Dialog({
			title: __("Send item changes to Shopify"),
			fields: [
				{
					fieldtype: "HTML",
					fieldname: "preview",
					options: `<p>${frappe.utils.escape_html(
						__("Shopify will update these item quantities. The customer will not be notified.")
					)}</p><ul>${rows}</ul><p>${frappe.utils.escape_html(
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
						method: SHOPIFY_EDIT_SEND_METHOD,
						type: "POST",
						args: { sales_order: frm.doc.name, fingerprint: preview.fingerprint },
						freeze: true,
						freeze_message: __("Sending item changes to Shopify..."),
					})
					.then((r) => {
						frappe.show_alert(shopify_edit_alert(r.message || {}));
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

	function show_shopify_edit_blocked(preview) {
		const reasons = (preview.reasons || [])
			.map((reason) => `<li>${frappe.utils.escape_html(reason)}</li>`)
			.join("");
		frappe.msgprint({
			title: __("Item changes cannot be sent to Shopify"),
			indicator: "orange",
			message: `<ul>${reasons}</ul>`,
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

	// Only a submitted, open Sales Order of a Shopify order the user may change; a call the
	// user is not permitted to make would open Frappe's "Not permitted" popup.
	function can_offer_shopify_edit(frm) {
		const doc = frm.doc;
		if (!doc || !doc.custom_marketplace_order || doc.docstatus !== 1) return false;
		if (is_closed_or_cancelled(doc)) return false;
		if (doc.custom_marketplace_platform && doc.custom_marketplace_platform !== "Shopify") return false;
		return Boolean(
			frappe.model.can_write("Sales Order") &&
				frappe.model.can_read("Marketplace Order") &&
				frappe.model.can_write("Marketplace Order")
		);
	}

	function add_shopify_edit_button(frm) {
		if (!can_offer_shopify_edit(frm)) return;
		const name = frm.doc.name;
		return frappe
			.call({ method: SHOPIFY_EDIT_PREVIEW_METHOD, type: "POST", args: { sales_order: name } })
			.then((r) => {
				// The form may have moved to another document while the call was in flight.
				if (frm.doc.name !== name) return;
				const preview = r.message;
				if (!preview) return;
				if (preview.state === "eligible") {
					frm.add_custom_button(__("Send item changes to Shopify"), () =>
						show_shopify_edit_dialog(frm, preview)
					);
				} else if (preview.state === "blocked" && (preview.changes || []).length) {
					// Staff changed items, so say why Shopify will not get them.  With no
					// changes there is nothing to explain and the form stays silent.
					frm.add_custom_button(__("Item changes blocked"), () =>
						show_shopify_edit_blocked(preview)
					);
				}
			})
			.catch(() => {});
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
	// Marketplace Order. Sets frm.__mi_noon_state to the pending state so the Marketplace Order
	// tab can list the shipments from the same call.
	function load_noon_fulfillment(frm) {
		frm.__mi_noon_state = null;
		const doc = frm.doc;
		if (
			!doc ||
			doc.docstatus !== 1 ||
			!doc.custom_marketplace_order ||
			doc.custom_marketplace_platform !== "Noon" ||
			!frappe.model.can_read("Marketplace Order")
		)
			return Promise.resolve(null);
		const load = frappe.require ? frappe.require(NOON_FULFILLMENT_ASSET).catch(() => {}) : Promise.resolve();
		const state = load.then(() => {
			const noon = noon_fulfillment_module();
			return noon ? noon.add_noon_fulfillment_actions(frm) : null;
		});
		frm.__mi_noon_state = state;
		return state;
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
				"Items and quantities of a Noon order cannot be changed here. Use the Noon Fulfillment buttons on this Sales Order (Mark Out of Stock or Cancel Shipment)."
			),
			"orange"
		);
	}

	function on_refresh(frm) {
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
		add_shopify_edit_button(frm);
		if (closed_by_click || is_closed_or_cancelled(frm.doc)) {
			load_platform_option(frm, { prompt: closed_by_click, button: true });
		}
	}

	function on_after_cancel(frm) {
		if (!is_marketplace_sales_order(frm.doc)) return;
		load_platform_option(frm, { prompt: true, button: false });
	}

	// The flag keeps the form handlers from being registered twice if the file is loaded twice.
	if (typeof frappe !== "undefined") {
		frappe.marketplace_integrations = frappe.marketplace_integrations || {};
		if (frappe.ui && frappe.ui.form && !frappe.ui.form.__mi_sales_order_hooks) {
			frappe.ui.form.__mi_sales_order_hooks = true;
			frappe.ui.form.on("Sales Order", {
				refresh: on_refresh,
				after_cancel: on_after_cancel,
			});
		}
	}

	if (typeof module !== "undefined") {
		module.exports = {
			add_marketplace_order_tab,
			add_noon_item_edit_hint,
			load_noon_fulfillment,
			add_shopify_edit_button,
			can_offer_shopify_edit,
			shopify_edit_alert,
			show_shopify_edit_blocked,
			show_shopify_edit_dialog,
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
			on_refresh,
			reason_field,
			result_alert,
			show_cancel_dialog,
			wrap_close_sales_order,
		};
	}
})();
