// Copyright (c) 2026, Tamwenat El Zekrayat and contributors
// For license information, please see license.txt

// Sales Order list extensions:
//
// - The order details hover card on every row (#325, order_hover_card.js): the helper is loaded with
//   frappe.require, the card is a convenience and failing to load it changes nothing else.
// - The carrier delivery counts column (epic #413, #409): custom_shipping_status_summary holds
//   {"Delivered": 2, "In transit between Hubs": 1}, stored by the Bosta and Noon status code, and is shown as
//   translated status pills (colors and the Partially Delivered rule: carrier_status.js).
// - For a marketplace order with a summary, the carrier status replaces the native To Deliver / Delivered
//   indicator (get_indicator); ERPNext's own indicator stays for every other order.
//
// Wrapped in a function: doctype_list_js files share one scope with ERPNext's own list script.
(function () {
	const CARD_METHOD = "marketplace_integrations.api.order_hover_card.get_sales_order_card";
	const CARD_ASSET = "/assets/marketplace_integrations/js/order_hover_card.js";
	const SUMMARY_FIELD = "custom_shipping_status_summary";
	// Orders ERPNext itself marks Closed / On Hold / unpaid-advance, drafts and cancelled keep their own indicator.
	const NATIVE_ONLY_STATUSES = ["Closed", "On Hold", "Cancelled", "Draft"];

	// carrier_status.js is loaded before this file (hooks.py doctype_list_js).
	function shared() {
		return (frappe.marketplace_integrations || {}).carrier_status;
	}

	function parse_summary(value) {
		return shared() ? shared().parse_summary(value) : [];
	}

	function format_shipping_status_summary(value) {
		return shared() ? shared().summary_html(shared().parse_summary(value)) : "";
	}

	// A marketplace order with a carrier summary shows the carrier status instead of ERPNext's
	// To Deliver / Delivered indicator; every other order, and an empty summary, keeps ERPNext's own.
	function external_indicator(doc) {
		if (!shared() || !doc || !doc.custom_marketplace_platform) return null;
		if (doc.docstatus !== 1 || NATIVE_ONLY_STATUSES.includes(doc.status)) return null;
		if (doc.advance_payment_status === "Requested") return null;
		const indicator = shared().indicator_for(shared().parse_summary(doc[SUMMARY_FIELD]));
		if (!indicator) return null;
		return [indicator[0], indicator[1], `custom_marketplace_platform,=,${doc.custom_marketplace_platform}`];
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
			const original_get_indicator = settings.get_indicator;
			settings.get_indicator = function (doc) {
				return external_indicator(doc) || (original_get_indicator ? original_get_indicator.call(this, doc) : undefined);
			};
			const original_onload = settings.onload;
			settings.onload = function (listview) {
				if (original_onload) original_onload.call(this, listview);
				add_hover_card(listview);
			};
			settings.__mi_list_extended = true;
		}
	}

	if (typeof module !== "undefined") {
		module.exports = { add_hover_card, external_indicator, format_shipping_status_summary, parse_summary };
	}
})();
