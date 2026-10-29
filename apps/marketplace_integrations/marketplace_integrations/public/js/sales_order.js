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
	const BOSTA_DEFAULTS_METHOD = "marketplace_integrations.api.bosta_shipments.get_create_defaults";
	const BOSTA_CITIES_METHOD = "marketplace_integrations.api.bosta_shipments.get_cities";
	const BOSTA_CREATE_METHOD = "marketplace_integrations.api.bosta_shipments.create_shipment";

	const BOSTA_CREATOR_ROLES = ["Sales Manager", "Stock User", "System Manager", "Bosta User"];
	// Marketplaces that fulfil their own orders get no Bosta action on their Sales Orders (#368).
	// Mirrors BOSTA_EXCLUDED_PLATFORMS in integrations/bosta/constants.py (a parity test keeps the two
	// equal). Add Amazon and Instashop here and there when those platforms are built.
	const BOSTA_EXCLUDED_PLATFORMS = ["Noon", "Talabat"];

	const BOSTA_SHIPMENT_METHOD = "marketplace_integrations.api.bosta_shipments.get_sales_order_shipment";
	const BOSTA_LABEL_METHOD = "marketplace_integrations.api.bosta_shipments.print_a4_arabic_label";
	const BOSTA_REFRESH_METHOD = "marketplace_integrations.api.bosta_shipments.refresh_shipment";
	const BOSTA_EDIT_METHOD = "marketplace_integrations.api.bosta_shipments.edit_shipment";
	// Bosta's documented states in which a delivery may be edited (OpenAPI, "Business update delivery").
	const BOSTA_EDITABLE_STATES = [10, 11, 20, 21, 22, 24, 30, 41, 47];
	const BOSTA_CANCEL_METHOD = "marketplace_integrations.api.bosta_shipments.cancel_shipment";
	const BOSTA_HANDOVER_METHOD = "marketplace_integrations.api.bosta_shipments.confirm_physical_handover";
	const BOSTA_HANDOVER_ROLES = ["System Manager", "Stock Manager"];
	const BOSTA_DELIVERED_STATE = 45;
	const BOSTA_TAB_BREAK_FIELD = "custom_bosta_delivery_tab";
	const BOSTA_TAB_FIELD = "custom_bosta_delivery_html";
	const MARKETPLACE_TAB_BREAK_FIELD = "custom_marketplace_order_tab";
	const MARKETPLACE_TAB_FIELD = "custom_marketplace_order_html";
	const NOON_TAB_FIELD = "custom_noon_fulfillment_html";
	const MARKETPLACE_SUMMARY_METHOD = "marketplace_integrations.api.sales_order_marketplace.get_marketplace_order_summary";
	const MARKETPLACE_REFRESH_METHODS = {
		Talabat: ["marketplace_integrations.api.talabat_orders.refresh_order", "Refresh from Talabat", "Refreshing from Talabat..."],
		Shopify: ["marketplace_integrations.api.shopify_orders.refresh_order", "Refresh from Shopify", "Refreshing from Shopify..."],
		Noon: ["marketplace_integrations.api.noon_orders.refresh_order", "Refresh from Noon", "Queuing Noon refresh..."],
	};
	const NOON_FULFILLMENT_ASSET = "/assets/marketplace_integrations/js/noon_fulfillment.js";
	const MARKETPLACE_SYNC_METHOD = "marketplace_integrations.api.marketplace_orders.sync_to_erpnext";

	function bosta_state_presentation(delivery_type, state_code, state_name, exception_reason, delivery_note_posted) {
		return frappe.marketplace_integrations && frappe.marketplace_integrations.bosta_state_presentation
			? frappe.marketplace_integrations.bosta_state_presentation(delivery_type, state_code, state_name, exception_reason, delivery_note_posted)
			: { kind: "state", label: state_name || "", hint: "" };
	}

	function bosta_state_html(shipment) {
		const escape = frappe.utils.escape_html;
		const view = bosta_state_presentation(shipment.bosta_delivery_type, shipment.bosta_state_code, shipment.bosta_state, shipment.bosta_exception_reason, shipment.delivery_note_posted);
		if (view.kind !== "stepper") return `<span class="${view.kind === "problem" || view.kind === "returned" ? "text-danger" : ""}">${escape(__(view.label))}</span>`;
		const steps = view.steps.map((step, index) => {
			const state = index < view.current_step ? "done" : index === view.current_step ? "current" : "future";
			return `<span class="bosta-progress-step bosta-progress-${state}">${escape(__(step.label))}</span>`;
		}).join(`<span class="bosta-progress-connector" aria-hidden="true">›</span>`);
		return `<div class="bosta-progress" aria-label="${escape(__("Bosta delivery progress"))}">${steps}</div>`;
	}

	function bosta_state_hint_html(shipment) {
		const escape = frappe.utils.escape_html;
		const view = bosta_state_presentation(shipment.bosta_delivery_type, shipment.bosta_state_code, shipment.bosta_state, shipment.bosta_exception_reason, shipment.delivery_note_posted);
		return view.hint ? `<span class="text-warning">${escape(__(view.hint))}</span>` : "";
	}

	function toggle_bosta_tab(frm, visible) {
		if (!frm.toggle_display) return;
		frm.toggle_display(BOSTA_TAB_BREAK_FIELD, visible);
		frm.toggle_display(BOSTA_TAB_FIELD, visible);
	}

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

	// Colours for the Bosta Shipment doctype's own statuses; the status text is the doctype's value.
	const BOSTA_STATUS_COLORS = {
		"Pending Bosta": "orange",
		"Bosta Created": "blue",
		"Delivery Note Failed": "red",
		Completed: "green",
		"Bosta Failed": "red",
		Canceled: "gray",
	};

	function is_bosta_excluded_platform(doc) {
		return Boolean(doc) && BOSTA_EXCLUDED_PLATFORMS.includes(doc.custom_marketplace_platform);
	}

	function can_create_bosta_shipment(frm) {
		return Boolean(
			frm.doc &&
				!is_bosta_excluded_platform(frm.doc) &&
				frm.doc.docstatus === 1 &&
				frm.doc.status !== "Cancelled" &&
				frappe.user &&
				frappe.user.has_role(BOSTA_CREATOR_ROLES)
		);
	}

	function start_create_bosta_shipment(frm) {
		frappe.call({ method: BOSTA_DEFAULTS_METHOD, type: "POST", args: { sales_order: frm.doc.name } }).then((r) => {
			show_bosta_dialog(frm, r.message || {});
		});
	}

	// Status, tracking number and Bosta state are stored/remote text: escaped before they
	// reach the dashboard (which renders raw HTML).
	function bosta_indicator_html(shipment) {
		const escape = frappe.utils.escape_html;
		const href = `/app/bosta-shipment/${encodeURIComponent(shipment.name)}`;
		let html = `<a href="${escape(href)}">${escape(__("Bosta Shipment"))}: ${escape(__(shipment.status))}</a>`;
		if (shipment.tracking_number) {
			html += ` &middot; ${escape(__("Tracking"))}: ${escape(shipment.tracking_number)}`;
		}
		if (shipment.bosta_state) html += ` (${escape(__(shipment.bosta_state))})`;
		return html;
	}

	function bosta_indicator_color(shipment) {
		const view = bosta_state_presentation(
			shipment.bosta_delivery_type,
			shipment.bosta_state_code,
			shipment.bosta_state,
			shipment.bosta_exception_reason,
			shipment.delivery_note_posted
		);
		return ["problem", "returned"].includes(view.kind) || view.hint
			? "red"
			: BOSTA_STATUS_COLORS[shipment.status] || "gray";
	}

	// The window is opened inside the click (a popup blocker only allows that) and pointed at
	// the file when the server answers. window.open with "noopener" returns null, so the
	// opener is cut by hand instead. If the browser blocks the window, the URL is offered as
	// a link; if the call fails or returns no file, the blank window is closed.
	function print_bosta_label(shipment) {
		const label_window = window.open("", "_blank");
		if (label_window) label_window.opener = null;
		const close = () => {
			if (label_window) label_window.close();
		};
		return frappe
			.call({
				method: BOSTA_LABEL_METHOD,
				type: "POST",
				args: { shipment: shipment.name },
				freeze: true,
				freeze_message: __("Preparing Bosta label..."),
			})
			.then((r) => {
				const label = r.message || {};
				if (!label.file_url) return close();
				if (label_window) {
					label_window.location.href = label.file_url;
					return;
				}
				const escape = frappe.utils.escape_html;
				frappe.msgprint({
					title: __("Bosta A4 Label"),
					message: `<a href="${escape(label.file_url)}" target="_blank" rel="noopener">${escape(
						__("Open the label")
					)}</a>`,
					indicator: "green",
				});
			})
			.catch(close);
	}

	// Refresh from Bosta (#271): the server reads the delivery and applies its state. A Bosta
	// error is thrown by the server (already escaped), shown by Frappe, and nothing is stored.
	function refresh_bosta_shipment(frm, shipment) {
		return frappe
			.call({
				method: BOSTA_REFRESH_METHOD,
				type: "POST",
				args: { shipment: shipment.name },
				freeze: true,
				freeze_message: __("Reading the delivery from Bosta..."),
			})
			.then(() => {
				frappe.show_alert({ message: __("Bosta state refreshed."), indicator: "green" });
				return frm.reload_doc();
			})
			.catch(() => {});
	}

	// Cancel Bosta Shipment (#272). Nothing is sent before the confirm is accepted; the confirm
	// text is HTML, so the tracking number and Delivery Note name are escaped into it.
	function cancel_bosta_shipment(frm, shipment) {
		const escape = frappe.utils.escape_html;
		const message = escape(
			__("Cancel Bosta delivery {0}? Bosta will terminate the carrier delivery. A submitted Delivery Note and posted stock will remain unchanged. This cannot be undone.", [
				shipment.tracking_number,
				shipment.delivery_note || "-",
			])
		);
		frappe.confirm(message, () => {
			frappe
				.call({
					method: BOSTA_CANCEL_METHOD,
					type: "POST",
					args: { shipment: shipment.name },
					freeze: true,
					freeze_message: __("Cancelling the Bosta delivery..."),
				})
				.then((r) => {
					const result = r.message || {};
					if (result.canceled) {
						frappe.show_alert({ message: __("Bosta delivery terminated. Posted stock remains unchanged."), indicator: "green" });
					} else {
						frappe.msgprint({
							title: __("Bosta Cancel"),
							message: escape(__("Bosta termination was confirmed, but the local shipment status needs review. No posted stock was reversed.")),
							indicator: "orange",
						});
					}
					return frm.reload_doc();
				})
				// A server error already showed Frappe's message; nothing changed.
				.catch(() => {});
		});
	}

	function bosta_row(label, value_html) {
		const escape = frappe.utils.escape_html;
		return `<tr><th style="width: 30%">${escape(label)}</th><td>${value_html}</td></tr>`;
	}

	function bosta_button(action, label) {
		const escape = frappe.utils.escape_html;
		return `<button class="btn btn-default btn-sm" style="margin-inline-end: 8px" data-bosta-action="${escape(action)}">${escape(label)}</button>`;
	}

	// The Bosta Delivery tab. Every stored or remote value is escaped. `options` is
	// { create, failed }: create = the user is a creator and may start a shipment; failed =
	// the shipment read failed (so "No Bosta shipment" would be a guess).
	function bosta_tab_html(shipment, options) {
		const escape = frappe.utils.escape_html;
		const { create, failed, read_only } = options || {};
		if (shipment && shipment.shipments) return bosta_multi_tab_html(shipment.shipments, { create, read_only });
		const buttons = [];
		let html = "";
		if (!shipment) {
			html += `<p class="text-muted">${escape(
				failed ? __("Could not load the Bosta shipment.") : __("No Bosta shipment")
			)}</p>`;
			if (create) buttons.push(bosta_button("create", __("Create Bosta Shipment")));
		} else {
			const text = (value) => (value ? escape(value) : "");
			const address = [shipment.address_first_line, shipment.city].filter(Boolean).join(", ");
			let delivery_note = "";
			if (shipment.delivery_note) {
				const href = `/app/delivery-note/${encodeURIComponent(shipment.delivery_note)}`;
				delivery_note = `<a href="${escape(href)}">${escape(shipment.delivery_note)}</a>`;
				if (shipment.delivery_note_status) delivery_note += ` (${escape(__(shipment.delivery_note_status))})`;
			}
			const cod = escape(format_currency(shipment.cod_amount || 0));
			const last_update = shipment.last_update ? escape(frappe.datetime.str_to_user(shipment.last_update)) : "";
			if (shipment.status === "Delivery Note Failed" && shipment.delivery_note_error) {
				html += `<p class="text-danger">${escape(shipment.delivery_note_error)}</p>`;
			}
			html += `<table class="table table-bordered"><tbody>`;
			html += bosta_row(__("Status"), text(__(shipment.status)));
			html += bosta_row(__("Tracking Number"), text(shipment.tracking_number));
			html += bosta_row(__("Bosta State"), bosta_state_html(shipment));
			if (bosta_state_hint_html(shipment)) html += bosta_row(__("Carrier Attention"), bosta_state_hint_html(shipment));
			html += bosta_row(__("Last Bosta Update"), last_update);
			html += bosta_row(__("COD Sent"), cod);
			if (shipment.shipment_fee !== null && shipment.shipment_fee !== undefined) {
				html += bosta_row(__("Bosta Delivery Fee"), escape(format_currency(shipment.shipment_fee, "EGP")));
			}
			html += bosta_row(__("Receiver"), [text(shipment.receiver_name), text(shipment.receiver_phone)].filter(Boolean).join(" - "));
			html += bosta_row(__("Address"), text(address));
			html += bosta_row(__("Delivery Note"), delivery_note);
			html += `</tbody></table>`;
			if (shipment.active && shipment.tracking_number) {
				buttons.push(bosta_button("print", __("Print A4 Label")));
				if (create || read_only) buttons.push(bosta_button("refresh", __("Refresh from Bosta")));
				if (create && BOSTA_EDITABLE_STATES.includes(shipment.bosta_state_code)) {
					buttons.push(bosta_button("edit", __("Edit Bosta Shipment")));
				}
				if (create && shipment.bosta_state_code !== BOSTA_DELIVERED_STATE) {
					buttons.push(bosta_button("cancel", __("Cancel Bosta Shipment")));
				}
			}
			// A Bosta Failed shipment without a tracking number keeps Create: that retry
			// (search Bosta, then send) is what the server resumes on the same row.
			if (create && (!shipment.active || (shipment.status === "Bosta Failed" && !shipment.tracking_number))) {
				buttons.push(bosta_button("create", __("Create Bosta Shipment")));
			}
		}
		return html + (buttons.length ? `<p>${buttons.join("")}</p>` : "");
	}

	function bosta_multi_tab_html(shipments, { create, read_only }) {
		const escape = frappe.utils.escape_html;
		const text = (value) => (value ? escape(value) : "");
		const buttons = [];
		let html = `<p class="text-muted">${escape(__("Bosta shipments: {0}", [shipments.length]))}</p>`;
		shipments.forEach((row) => {
			const address = [row.address_first_line, row.city].filter(Boolean).join(", ");
			let delivery_note = "";
			if (row.delivery_note) {
				const href = `/app/delivery-note/${encodeURIComponent(row.delivery_note)}`;
				delivery_note = `<a href="${escape(href)}">${escape(row.delivery_note)}</a>`;
				if (row.delivery_note_status) delivery_note += ` (${escape(__(row.delivery_note_status))})`;
			}
			const last_update = row.last_update ? escape(frappe.datetime.str_to_user(row.last_update)) : "";
			html += `<details${shipments.length === 1 ? " open" : ""}><summary><bdi dir="ltr">${text(row.name)}</bdi> · ${text(row.tracking_number || __("No tracking number"))} · ${text(__(row.bosta_state || row.status))}</summary><table class="table table-bordered"><tbody>`;
			html += bosta_row(__("Shipment ID"), text(row.name));
			html += bosta_row(__("Carrier Status"), row.bosta_state ? bosta_state_html(row) : text(__(row.status)));
			if (bosta_state_hint_html(row)) html += bosta_row(__("Carrier Attention"), bosta_state_hint_html(row));
			html += bosta_row(__("Tracking Number"), text(row.tracking_number));
			html += bosta_row(__("Last Bosta Update"), last_update);
			html += bosta_row(__("COD Sent"), escape(format_currency(row.cod_amount || 0)));
			if (row.shipment_fee !== null && row.shipment_fee !== undefined) {
				html += bosta_row(__("Bosta Delivery Fee"), escape(format_currency(row.shipment_fee, "EGP")));
			}
			html += bosta_row(__("Receiver"), [text(row.receiver_name), text(row.receiver_phone)].filter(Boolean).join(" - "));
			html += bosta_row(__("Address"), text(address));
			html += bosta_row(__("Handover"), text(__(row.handover_status)));
			html += bosta_row(__("Stock Posting"), text(__(row.stock_posting_status)));
			html += bosta_row(__("Delivery Note"), delivery_note);
			if (row.allocations && row.allocations.length) {
				const allocations = row.allocations.map((a) => `${text(a.item_code)}: ${text(a.qty)} ${text(a.uom)} · ${text(a.warehouse)}`).join("<br>");
				html += bosta_row(__("Reserved allocations"), allocations);
			}
			html += `</tbody></table></details>`;
			if (row.status === "Delivery Note Failed" && row.delivery_note_error) html += `<p class="text-danger">${escape(row.delivery_note_error)}</p>`;
			if (row.active && row.tracking_number) {
				buttons.push(bosta_button(`print:${row.name}`, __("Print A4 Label")));
				if (create || read_only) buttons.push(bosta_button(`refresh:${row.name}`, __("Track Bosta Shipment")));
				if (create && BOSTA_EDITABLE_STATES.includes(row.bosta_state_code)) buttons.push(bosta_button(`edit:${row.name}`, __("Edit Bosta Shipment")));
				if (create && row.bosta_state_code !== BOSTA_DELIVERED_STATE) buttons.push(bosta_button(`cancel:${row.name}`, __("Cancel Bosta Shipment")));
			}
			if (!read_only && BOSTA_HANDOVER_ROLES.some((role) => frappe.user.has_role(role)) && row.active && (row.handover_status === "Awaiting Handover" || row.stock_posting_status === "Failed")) {
				buttons.push(bosta_button(`handover:${row.name}`, __(row.handover_status === "Awaiting Handover" ? "Confirm Physical Handover" : "Retry Stock Posting")));
			}
			if (create && row.active && row.status === "Bosta Failed" && !row.tracking_number) buttons.push(bosta_button("create", __("Retry Bosta Shipment")));
		});
		if (create && !shipments.some((row) => row.active)) buttons.push(bosta_button("create", __("Create Bosta Shipment")));
		return html + (buttons.length ? `<p>${buttons.join("")}</p>` : "");
	}

	// Draws the tab and wires its buttons (delegated, so a redraw never stacks handlers).
	function render_bosta_tab(frm, shipment, options) {
		const field = frm.get_field && frm.get_field(BOSTA_TAB_FIELD);
		if (!field || !field.$wrapper) return;
		field.$wrapper.html(bosta_tab_html(shipment, options));
		field.$wrapper.off("click.mi_bosta").on("click.mi_bosta", "[data-bosta-action]", (event) => {
			const [action, shipment_name] = event.currentTarget.getAttribute("data-bosta-action").split(":");
			const current = shipment_name
				? shipment && (shipment.shipments || [shipment]).find((row) => row.name === shipment_name)
				: shipment;
			if (action === "create") start_create_bosta_shipment(frm);
			else if (action === "print") print_bosta_label(current);
			else if (action === "refresh") refresh_bosta_shipment(frm, current);
			else if (action === "cancel") cancel_bosta_shipment(frm, current);
			else if (action === "edit") show_bosta_edit_dialog(frm, current);
			else if (action === "handover") confirm_bosta_handover(frm, current);
		});
	}

	function confirm_bosta_handover(frm, shipment) {
		if (!shipment) return;
		const escape = frappe.utils.escape_html;
		const allocations = (shipment.allocations || []).map((row) =>
			`<li>${escape(row.item_code)} — ${escape(row.qty)} ${escape(row.uom)} · ${escape(row.warehouse)}</li>`
		).join("");
		const message = `<p>${escape(__("Confirm that this Bosta shipment has physically left the warehouse? This posts its reserved stock once and creates a Delivery Note."))}</p><p>${escape(__("Shipment"))}: <bdi dir="ltr">${escape(shipment.name)}</bdi><br>${escape(__("Tracking Number"))}: <bdi dir="ltr">${escape(shipment.tracking_number || "-")}</bdi><br>${escape(__("Warehouse allocations"))}:</p><ul>${allocations}</ul>`;
		frappe.confirm(message, () => {
			frappe.call({
				method: BOSTA_HANDOVER_METHOD,
				type: "POST",
				args: { shipment: shipment.name },
				freeze: true,
				freeze_message: __("Posting the reserved stock..."),
			}).then((r) => {
				const result = r.message || {};
				const confirmed_on = result.handover_confirmed_on
					? frappe.datetime.str_to_user(result.handover_confirmed_on)
					: "-";
				frappe.msgprint({
					title: __("Physical handover recorded"),
					message: escape(__("Handover was confirmed by {0} at {1}. Carrier status: {2}. Stock posting status: {3}. Delivery Note: {4}.", [result.handover_confirmed_by || "-", confirmed_on, shipment.bosta_state || shipment.status || "-", result.stock_posting_status || __("Unknown"), result.delivery_note || "-"])),
					indicator: result.delivery_note ? "green" : "orange",
				});
				return frm.reload_doc();
			}).catch(() => {});
		});
	}

	// The form clears custom buttons and the dashboard indicators on every refresh
	// (Form.refresh), so nothing here is remembered: it is all added again each time. The
	// current shipment is read from the server; a reply that arrives after a newer refresh
	// (or for another document) is dropped, so the indicator is never added twice. A user
	// who cannot read Bosta Shipment never asks and sees nothing in the tab. If the read
	// fails, Create stays available to creators in the tab (the server still allows one
	// active shipment per order).
	function add_bosta_shipment_ux(frm) {
		if (!frm.doc || !frm.doc.name || frm.doc.docstatus < 1) return Promise.resolve();
		const request = (frm.__mi_bosta_request = (frm.__mi_bosta_request || 0) + 1);
		const sales_order = frm.doc.name;
		const create = can_create_bosta_shipment(frm);
		// A Sales Order of a platform that fulfils its own orders keeps showing an existing shipment
		// (state, label, refresh) but offers no create, edit, cancel or handover.
		const read_only =
			is_bosta_excluded_platform(frm.doc) &&
			Boolean(frappe.user && frappe.user.has_role(BOSTA_CREATOR_ROLES));
		if (!(frappe.model && frappe.model.can_read("Bosta Shipment"))) {
			const field = frm.get_field && frm.get_field(BOSTA_TAB_FIELD);
			if (field && field.$wrapper) field.$wrapper.html("");
			toggle_bosta_tab(frm, false);
			return Promise.resolve();
		}
		let failed = false;
		const load_presentation = frappe.require
			? frappe.require("/assets/marketplace_integrations/js/bosta_state_presentation.js").catch(() => {})
			: Promise.resolve();
		return load_presentation.then(() => frappe
			.call({ method: BOSTA_SHIPMENT_METHOD, type: "POST", args: { sales_order }, silent: true })
			.then(
				(r) => r.message || null,
				() => {
					failed = true;
					return null;
				}
			)
			.then((shipment) => {
				if (request !== frm.__mi_bosta_request || frm.doc.name !== sales_order) return;
				const shipments = shipment && (shipment.shipments || [shipment]);
				const active = shipments && shipments.find((row) => row.active);
				if (active && frm.dashboard) {
					frm.dashboard.add_indicator(
						bosta_indicator_html(active),
						bosta_indicator_color(active)
					);
				}
				if (shipment) {
					toggle_bosta_tab(frm, true);
					render_bosta_tab(frm, shipment, { create, failed, read_only });
					return;
				}
				if (!create) {
					toggle_bosta_tab(frm, false);
					return;
				}
				// Existing Bosta defaults/validation are the authority for eligibility; do not
				// render a shipment tab for arbitrary Sales Orders.
				frappe.call({ method: BOSTA_DEFAULTS_METHOD, type: "POST", args: { sales_order }, silent: true }).then(
					() => {
						if (request !== frm.__mi_bosta_request || frm.doc.name !== sales_order) return;
						toggle_bosta_tab(frm, true);
						render_bosta_tab(frm, null, { create, failed });
					},
					() => {
						if (request === frm.__mi_bosta_request && frm.doc.name === sales_order) {
							toggle_bosta_tab(frm, false);
						}
					}
				);
			}));
	}

	// Bosta city names are remote text. Frappe renders an Autocomplete label as
	// dropdown HTML and also uses it as the input text, so escaping "&" would show "&amp;"
	// after picking. Only "<" and ">" are swapped for look-alikes: a label can then never
	// open a tag, and "&" stays readable.
	function safe_text(text) {
		return String(text).replace(/</g, "\u2039").replace(/>/g, "\u203a");
	}
	function location_label(item) {
		return [item.name, item.name_ar].filter(Boolean).map(safe_text).join(" - ");
	}

	function location_options(items) {
		return items.map((item) => ({ value: item.id, label: location_label(item) }));
	}

	function match_city(cities, source_city) {
		const wanted = String(source_city || "").trim().toLowerCase();
		if (!wanted) return null;
		return cities.find((city) => city.name.trim().toLowerCase() === wanted) || null;
	}

	function show_bosta_dialog(frm, defaults) {
		return open_create_dialog({
			defaults,
			submit(dialog, values, city) {
				dialog.hide();
				frappe.call({
					method: BOSTA_CREATE_METHOD,
					type: "POST",
					args: {
						sales_order: frm.doc.name,
						receiver_name: values.receiver_name,
						receiver_phone: values.receiver_phone,
						city_id: city.id,
						address_first_line: values.address_first_line,
						expected_cod: defaults.cod_amount,
						cod_amount: values.cod_amount,
					},
					freeze: true,
					freeze_message: __("Creating Bosta shipment..."),
				}).then(() => frm.reload_doc());
			},
		});
	}

	// The Create Bosta Shipment dialog, shared with the Sales Order list's bulk create
	// (#305, sales_order_list.js). `submit(dialog, values, city)` runs once a city is chosen.
	// Bulk also passes `progress` ("Order 2 of 5"), `on_skip` (adds a Skip button)
	// and `on_hide` (the dialog was closed without submit or skip).
	function open_create_dialog({ defaults, submit, progress, on_skip, on_hide }) {
		const intro_fields = [];
		if (progress) {
			intro_fields.push({
				fieldtype: "HTML",
				fieldname: "progress_note",
				options: `<p><b>${frappe.utils.escape_html(progress)}</b></p>`,
			});
		}
		intro_fields.push({ fieldtype: "HTML", fieldname: "cod_note", options: bosta_cod_note_html(defaults) });
		return open_bosta_location_dialog({
			title: __("Create Bosta Shipment"),
			primary_action_label: __("Create Shipment"),
			defaults,
			intro_fields,
			receiver_fields: [
				{ fieldname: "currency", fieldtype: "Data", hidden: 1, default: defaults.currency },
				{
					fieldname: "cod_amount",
					fieldtype: "Currency",
					label: __("COD Amount"),
					reqd: 1,
					default: defaults.retry_cod_amount ?? defaults.cod_amount,
					// A retry resumes the existing shipment, which keeps the COD already used.
					read_only: defaults.retry_cod_amount === undefined || defaults.retry_cod_amount === null ? 0 : 1,
					options: "currency",
					description: __(
						"Enter an amount from 0 to {0} in the company currency.",
						[format_currency(defaults.cod_limit || 30000, defaults.currency)]
					),
				},
				{ fieldname: "receiver_name", fieldtype: "Data", label: __("Receiver Name"), reqd: 1, default: defaults.receiver_name },
				{ fieldname: "receiver_phone", fieldtype: "Data", label: __("Receiver Phone"), reqd: 1, default: defaults.receiver_phone },
			],
			submit,
			on_skip,
			on_hide,
		});
	}

	// Edit Bosta Shipment (#273): the same city picker as the create dialog, with only the
	// receiver phone and address; no district, Bosta resolves it (#318). The values shown are
	// stored text, so they only ever go into field defaults (never HTML). Nothing is sent until
	// the confirm is accepted.
	function show_bosta_edit_dialog(frm, shipment) {
		const defaults = {
			address_city: shipment.city,
			city_id: shipment.city_id,
			address_first_line: shipment.address_first_line,
		};
		const note = __(
			"This updates Bosta and the Bosta Shipment only. The Sales Order address and Shopify are not changed; update them by hand if needed."
		);
		return open_bosta_location_dialog({
			title: __("Edit Bosta Shipment"),
			primary_action_label: __("Update Shipment"),
			defaults,
			intro_fields: [
				{ fieldtype: "HTML", fieldname: "edit_note", options: `<p>${frappe.utils.escape_html(note)}</p>` },
			],
			receiver_fields: [
				{ fieldname: "receiver_phone", fieldtype: "Data", label: __("Receiver Phone"), reqd: 1, default: shipment.receiver_phone },
			],
			submit(dialog, values, city) {
				const message = frappe.utils.escape_html(
					__("Update Bosta delivery {0}? {1}", [shipment.tracking_number, note])
				);
				frappe.confirm(message, () => {
					dialog.hide();
					frappe
						.call({
							method: BOSTA_EDIT_METHOD,
							type: "POST",
							args: {
								shipment: shipment.name,
								receiver_phone: values.receiver_phone,
								city_id: city.id,
								address_first_line: values.address_first_line,
							},
							freeze: true,
							freeze_message: __("Updating the Bosta delivery..."),
						})
						.then(() => {
							frappe.show_alert({ message: __("Bosta delivery updated."), indicator: "green" });
							return frm.reload_doc();
						})
						// A server error already showed Frappe's message; nothing changed.
						.catch(() => {});
				});
			},
		});
	}

	// The dialog shell shared by Create and Edit: leading fields, receiver fields, then the
	// Bosta city picker and the address first line. No district: Bosta resolves it from the
	// city and the address (#318). `submit(dialog, values, city)` runs once a city is chosen.
	function open_bosta_location_dialog({ title, primary_action_label, defaults, intro_fields, receiver_fields, submit, on_skip, on_hide }) {
		const state = { cities: [] };
		const dialog = new frappe.ui.Dialog({
			title,
			fields: [
				...intro_fields,
				{ fieldtype: "HTML", fieldname: "lists_error", options: "", hidden: 1 },
				{ fieldtype: "Button", fieldname: "retry_lists", label: __("Retry"), hidden: 1, click: () => load_cities(false) },
				...receiver_fields,
				// Autocomplete: searchable, shows the name label and stores the Bosta ID.
				{ fieldname: "city", fieldtype: "Autocomplete", label: __("City"), reqd: 1, max_items: 1000, options: [] },
				{ fieldname: "address_first_line", fieldtype: "Small Text", label: __("Address First Line"), reqd: 1, default: defaults.address_first_line },
			],
			primary_action_label,
			primary_action(values) {
				const city = state.cities.find((c) => c.id === values.city);
				if (!city) return;
				submit(dialog, values, city);
			},
			secondary_action_label: __("Refresh Bosta Lists"),
			secondary_action() {
				load_cities(true);
			},
		});

		if (on_skip && dialog.add_custom_action) {
			dialog.add_custom_action(__("Skip"), () => on_skip(dialog));
		}
		if (on_hide) dialog.onhide = on_hide;

		function set_ready(ready, error_text) {
			const error = !ready && Boolean(error_text);
			dialog.get_primary_btn().prop("disabled", !ready);
			dialog.set_df_property("lists_error", "hidden", error ? 0 : 1);
			dialog.set_df_property("retry_lists", "hidden", error ? 0 : 1);
			if (error) {
				dialog.set_df_property(
					"lists_error",
					"options",
					`<p class="text-danger">${frappe.utils.escape_html(error_text)}</p>`
				);
			}
		}

		function load_cities(refresh) {
			set_ready(false);
			return frappe
				.call({ method: BOSTA_CITIES_METHOD, type: "POST", silent: true, args: { refresh: refresh ? 1 : 0 } })
				.then((r) => {
					state.cities = r.message || [];
					dialog.fields_dict.city.set_data(location_options(state.cities));
					set_ready(true);
					// Refreshing keeps the chosen city.
					const selected = dialog.get_value("city");
					if (selected && state.cities.some((c) => c.id === selected)) return;
					dialog.set_value("city", "");
					const match =
						state.cities.find((c) => c.id === defaults.city_id) ||
						match_city(state.cities, defaults.address_city);
					if (match) return dialog.set_value("city", match.id);
				})
				.catch(() =>
					set_ready(false, __("Could not load the Bosta {0}. Check Bosta Settings and try again.", [__("cities")]))
				);
		}

		dialog.show();
		return load_cities(false);
	}

	// The COD that will be sent is the outstanding amount (total minus payments). Values are
	// numbers from the server; formatted and escaped before they reach the HTML field.
	function bosta_cod_note_html(defaults) {
		const escape = frappe.utils.escape_html;
		const format = (value) => escape(format_currency(value || 0));
		let html = `<p><b>${escape(__("COD to be sent to Bosta"))}: ${format(defaults.cod_amount)}</b></p>`;
		if (!defaults.cod_amount) {
			html += `<p>${escape(__("This amount is 0. Bosta may reject a delivery without COD."))}</p>`;
		}
		html += `<p>${escape(
			__(
				"COD is the Sales Order total minus advance payments recorded on the Sales Order. Payments made against a Sales Invoice are not seen, and a partly delivered order still uses the whole-order amount."
			)
		)}</p>`;
		const retry_cod = defaults.retry_cod_amount;
		if (retry_cod !== undefined && retry_cod !== null && Number(retry_cod) !== Number(defaults.cod_amount || 0)) {
			html += `<p><b>${escape(
				__("This retry sends the COD entered earlier, {0}, not the current outstanding amount, {1}.", [
					format_currency(retry_cod),
					format_currency(defaults.cod_amount || 0),
				])
			)}</b></p>`;
		}
		if (defaults.advance_paid) {
			html += `<p>${escape(
				__("Order total {0}, less payments already received {1}.", [
					format_currency(defaults.order_total || 0),
					format_currency(defaults.advance_paid || 0),
				])
			)}</p>`;
		}
		return html;
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
	// Marketplace Order. Sets frm.__mi_noon_state to the pending state so the Noon Fulfillment
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

	// #378: the Noon Fulfillment tab lists the Sales Invoice and one block per shipment. It uses
	// the state load_noon_fulfillment fetched (the same call that drives the buttons), so it waits
	// for it. The tab itself is shown by the field's depends_on (Noon platform only).
	function render_noon_fulfillment_tab(frm) {
		const field = frm.get_field && frm.get_field(NOON_TAB_FIELD);
		if (!field || !field.$wrapper) return Promise.resolve();
		if (!frm.doc || frm.doc.custom_marketplace_platform !== "Noon") {
			field.$wrapper.html("");
			return Promise.resolve();
		}
		const name = frm.doc.name;
		const request = (frm.__mi_noon_tab_request = (frm.__mi_noon_tab_request || 0) + 1);
		return (frm.__mi_noon_state || Promise.resolve(null)).then((state) => {
			if (request !== frm.__mi_noon_tab_request || frm.doc.name !== name) return;
			const noon = noon_fulfillment_module();
			if (noon) field.$wrapper.html(noon.noon_fulfillment_tab_html(state));
			else field.$wrapper.html(`<p class="text-muted">${frappe.utils.escape_html(__("Noon fulfillment is not available for this order."))}</p>`);
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
				"Items and quantities of a Noon order cannot be changed here. Use the Noon Fulfillment buttons on this Sales Order (Mark Out of Stock or Cancel Shipment)."
			),
			"orange"
		);
	}

	function on_refresh(frm) {
		load_noon_fulfillment(frm);
		render_noon_fulfillment_tab(frm);
		add_marketplace_order_tab(frm);
		add_bosta_shipment_ux(frm);
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

	// The Sales Order list loads this file (frappe.require) for the dialog; the flag keeps the
	// form handlers from being registered twice when both the list and the form load it.
	if (typeof frappe !== "undefined") {
		frappe.marketplace_integrations = frappe.marketplace_integrations || {};
		frappe.marketplace_integrations.open_bosta_create_dialog = open_create_dialog;
		frappe.marketplace_integrations.BOSTA_CREATOR_ROLES = BOSTA_CREATOR_ROLES;
		frappe.marketplace_integrations.BOSTA_EXCLUDED_PLATFORMS = BOSTA_EXCLUDED_PLATFORMS;
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
			add_bosta_shipment_ux,
			add_noon_item_edit_hint,
			load_noon_fulfillment,
			render_noon_fulfillment_tab,
			add_shopify_edit_button,
			can_offer_shopify_edit,
			shopify_edit_alert,
			show_shopify_edit_blocked,
			show_shopify_edit_dialog,
			bosta_cod_note_html,
			bosta_indicator_html,
			bosta_tab_html,
			bosta_state_html,
			location_options,
			match_city,
			open_create_dialog,
			show_bosta_dialog,
			show_bosta_edit_dialog,
			blocked_message,
			can_offer_cancel,
			is_bosta_excluded_platform,
			BOSTA_EXCLUDED_PLATFORMS,
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
