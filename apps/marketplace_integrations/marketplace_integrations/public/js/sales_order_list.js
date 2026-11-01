// Copyright (c) 2026, Tamwenat El Zekrayat and contributors
// For license information, please see license.txt

// Sales Order list extensions:
//
// - The order details hover card on every row (#325, order_hover_card.js): the helper is loaded with
//   frappe.require, the card is a convenience and failing to load it changes nothing else.
// - The Bosta delivery counts column (epic #413, #409): custom_shipping_status_summary holds
//   {"Delivered": 2, "In transit between Hubs": 1}, stored by the Bosta status job, and is shown as
//   translated status pills with counts.
//
// Wrapped in a function: doctype_list_js files share one scope with ERPNext's own list script.
(function () {
	const CARD_METHOD = "marketplace_integrations.api.order_hover_card.get_sales_order_card";
	const CARD_ASSET = "/assets/marketplace_integrations/js/order_hover_card.js";
	const SUMMARY_FIELD = "custom_shipping_status_summary";
	const STATUS_COLORS = {
		Delivered: "green",
		Fulfilled: "green",
		Created: "blue",
		"Pickup requested": "blue",
		"Waiting for route": "orange",
		"Route Assigned": "blue",
		"Picked up": "blue",
		"Picked up from business": "blue",
		"Received at warehouse": "blue",
		"In transit between Hubs": "blue",
		"Returned to business": "orange",
		"Returned to stock": "orange",
		"On hold": "orange",
		Exception: "red",
		Lost: "red",
		Damaged: "red",
		Investigation: "red",
		"Awaiting your action": "red",
		Terminated: "red",
		Canceled: "grey",
		Archived: "grey",
	};

	// The stored value is JSON written by the server; parse it defensively and keep only
	// {text: positive integer} entries.
	function parse_summary(value) {
		let data = value;
		if (typeof value === "string") {
			try {
				data = JSON.parse(value);
			} catch (error) {
				return [];
			}
		}
		if (!data || typeof data !== "object" || Array.isArray(data)) return [];
		return Object.entries(data).filter(([, count]) => Number.isInteger(count) && count > 0);
	}

	function format_shipping_status_summary(value) {
		const escape = frappe.utils.escape_html;
		return parse_summary(value)
			.map(([status, count]) => {
				const color = STATUS_COLORS[status] || "grey";
				return `<span class="indicator-pill ${color}">${escape(__(status))} <bdi dir="ltr">${escape(count)}</bdi></span>`;
			})
			.join(" ");
	}

	function add_hover_card(listview) {
		return frappe
			.require(CARD_ASSET)
			.then(() => {
				frappe.marketplace_integrations.order_hover_card
					.create({ method: CARD_METHOD, arg: "sales_order" })
					.attach_to_list(listview);
			})
			.catch((error) => console.error("Order hover card failed to load (#325).", error));
	}

	// Add to ERPNext's own Sales Order list settings instead of replacing them.
	if (typeof frappe !== "undefined" && frappe.listview_settings) {
		const settings = (frappe.listview_settings["Sales Order"] = frappe.listview_settings["Sales Order"] || {});
		settings.add_fields = [...new Set([...(settings.add_fields || []), "custom_marketplace_platform", SUMMARY_FIELD])];
		settings.formatters = {
			...(settings.formatters || {}),
			[SUMMARY_FIELD]: format_shipping_status_summary,
		};
		// Marks the settings as already extended, so loading this file again (route revisit, asset
		// reload) never wraps onload twice.
		if (!settings.__mi_list_extended) {
			const original_onload = settings.onload;
			settings.onload = function (listview) {
				if (original_onload) original_onload.call(this, listview);
				add_hover_card(listview);
			};
			settings.__mi_list_extended = true;
		}
	}

	if (typeof module !== "undefined") {
		module.exports = { add_hover_card, format_shipping_status_summary, parse_summary };
	}
})();
