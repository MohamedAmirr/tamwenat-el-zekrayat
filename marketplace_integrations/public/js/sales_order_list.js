// Copyright (c) 2026, Tamwenat El Zekrayat and contributors
// For license information, please see license.txt

// Sales Order list extensions:
//
// - The order details hover card on every row (#325, order_hover_card.js): the helper is loaded with
//   frappe.require, the card is a convenience and failing to load it changes nothing else.
// - The carrier delivery counts column (epic #413, #409): custom_shipping_status_summary holds
//   {"Delivered": 2, "In transit between Hubs": 1}, stored by the Bosta and Noon status code, and is shown as
//   translated status pills (colors and the Partially Delivered rule: carrier_status.js).
// - The delivery countdown column (custom_earliest_delivery_date): days left to the earliest item delivery
//   date, open marketplace orders only (format_delivery_countdown).
// - For a marketplace order with a summary, the carrier status replaces the native To Deliver / Delivered
//   indicator (get_indicator); ERPNext's own indicator stays for every other order.
//
// - The bulk "Delivery Note" action of the Actions menu opens the shipping method dialog
//   (sales_order_bulk_delivery.js) in place of ERPNext's own bulk action, which is dropped.
//
// Wrapped in a function: doctype_list_js files share one scope with ERPNext's own list script.
(function () {
	const CARD_METHOD = "marketplace_integrations.api.order_hover_card.get_sales_order_card";
	const CARD_ASSET = "/assets/marketplace_integrations/js/order_hover_card.js";
	const SUMMARY_FIELD = "custom_shipping_status_summary";
	const PLATFORM_FIELD = "custom_marketplace_platform";
	const EARLIEST_FIELD = "custom_earliest_delivery_date";
	const NO_SHIPMENT = "No shipment yet";
	// Orders ERPNext itself marks Closed / On Hold / unpaid-advance, drafts and cancelled keep their own indicator.
	const NATIVE_ONLY_STATUSES = ["Closed", "On Hold", "Cancelled", "Draft"];
	// The "No shipment yet" placeholder shows only while the order is still waiting to be delivered.
	const AWAITING_DELIVERY_STATUSES = ["To Deliver", "To Deliver and Bill"];

	// carrier_status.js is loaded before this file (hooks.py doctype_list_js).
	function shared() {
		return (frappe.marketplace_integrations || {}).carrier_status;
	}

	function parse_summary(value) {
		return shared() ? shared().parse_summary(value) : [];
	}

	// platform_badge.js is loaded before this file too; looked up on use like carrier_status.
	function format_platform(value, df) {
		const badge = (frappe.marketplace_integrations || {}).platform_badge;
		return badge ? badge.formatter(value, df) : frappe.utils.escape_html(value || "");
	}

	// The summary pills; for a submitted marketplace order still To Deliver / To Deliver and Bill with no carrier
	// status yet, one grey placeholder pill so the column explains the empty state. Every other status and
	// non-marketplace orders show nothing.
	function format_shipping_status_summary(value, df, doc) {
		if (!shared()) return "";
		const entries = shared().parse_summary(value);
		if (entries.length) return shared().summary_html(entries);
		if (
			doc &&
			doc.custom_marketplace_platform &&
			doc.docstatus === 1 &&
			AWAITING_DELIVERY_STATUSES.includes(doc.status)
		)
			return shared().pill(NO_SHIPMENT);
		return "";
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

	// The delivery countdown column: custom_earliest_delivery_date is the earliest item delivery date, set by the
	// sync (ERPNext's own header delivery_date is the LATEST item date). Only an open marketplace order shows a
	// pill; the whole-day difference comes from frappe.datetime, never from browser timezone math.
	function format_delivery_countdown(value, df, doc) {
		if (
			!value ||
			!doc ||
			!doc.custom_marketplace_platform ||
			doc.docstatus !== 1 ||
			!AWAITING_DELIVERY_STATUSES.includes(doc.status)
		)
			return "";
		const days = frappe.datetime.get_diff(value, frappe.datetime.get_today());
		if (!Number.isFinite(days)) return "";
		let text;
		let color;
		if (days >= 2) [text, color] = [__("In {0} days", [days]), "green"];
		else if (days === 1) [text, color] = [__("Tomorrow"), "orange"];
		else if (days === 0) [text, color] = [__("Today"), "orange"];
		else if (days === -1) [text, color] = [__("Overdue 1 day"), "red"];
		else [text, color] = [__("Overdue {0} days", [-days]), "red"];
		const title = frappe.utils.escape_html(frappe.datetime.str_to_user(value));
		return `<span class="indicator-pill ${color}" title="${title}">${frappe.utils.escape_html(text)}</span>`;
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
		settings.add_fields = [...new Set([...(settings.add_fields || []), PLATFORM_FIELD, SUMMARY_FIELD, EARLIEST_FIELD])];
		settings.formatters = {
			...(settings.formatters || {}),
			[SUMMARY_FIELD]: format_shipping_status_summary,
			[EARLIEST_FIELD]: format_delivery_countdown,
			[PLATFORM_FIELD]: format_platform,
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
				const run_original = () => original_onload && original_onload.call(this, listview);
				// ERPNext's onload adds its own bulk "Delivery Note" action; the item with that label opens our
				// dialog instead and ERPNext's callback is dropped (sales_order_bulk_delivery.js, an app include).
				// Without it, ERPNext's stays as it is.
				const bulk = (frappe.marketplace_integrations || {}).bulk_delivery;
				if (bulk) {
					bulk.listen();
					bulk.run_with_replaced_action(listview, run_original);
				} else {
					run_original();
				}
				add_hover_card(listview);
			};
			settings.__mi_list_extended = true;
		}
	}

	if (typeof module !== "undefined") {
		module.exports = {
			add_hover_card,
			external_indicator,
			format_delivery_countdown,
			format_platform,
			format_shipping_status_summary,
			parse_summary,
		};
	}
})();
