// Copyright (c) 2026, Tamwenat El Zekrayat and contributors
// For license information, please see license.txt

// Delivery Note list: the external carrier status (Bosta state or Noon Shipping Status) as a colored pill
// in its own column next to ERPNext's native status. The column exists because Delivery Note-custom_shipping_status
// is in_list_view (fixtures/custom_field.json); this file only formats it. A note without a carrier status
// shows nothing. Colors: carrier_status.js, loaded before this file (hooks.py doctype_list_js).
//
// The native Carrier (`transporter`, a Supplier name) is a list column too (Property Setter
// Delivery Note-transporter-in_list_view, fixtures/property_setter.json) shown as a colored pill:
// Noon yellow, Bosta red, any other carrier grey (platform_badge.js, also loaded before this file).
//
// Wrapped in a function: doctype_list_js files share one scope with ERPNext's own list script.
(function () {
	const STATUS_FIELD = "custom_shipping_status";
	const CARRIER_FIELD = "transporter";

	function format_carrier_status(value) {
		const shared = (frappe.marketplace_integrations || {}).carrier_status;
		return shared ? shared.status_pill(value) : "";
	}

	function format_carrier(value, df) {
		const badge = (frappe.marketplace_integrations || {}).platform_badge;
		return badge ? badge.carrier_formatter(value, df) : frappe.utils.escape_html(value || "");
	}

	// Add to ERPNext's own Delivery Note list settings instead of replacing them.
	if (typeof frappe !== "undefined" && frappe.listview_settings) {
		const settings = (frappe.listview_settings["Delivery Note"] = frappe.listview_settings["Delivery Note"] || {});
		settings.add_fields = [...new Set([...(settings.add_fields || []), STATUS_FIELD, CARRIER_FIELD])];
		settings.formatters = {
			...(settings.formatters || {}),
			[STATUS_FIELD]: format_carrier_status,
			[CARRIER_FIELD]: format_carrier,
		};
	}

	if (typeof module !== "undefined") {
		module.exports = { format_carrier, format_carrier_status };
	}
})();
