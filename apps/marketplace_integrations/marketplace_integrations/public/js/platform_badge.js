// Copyright (c) 2026, Tamwenat El Zekrayat and contributors
// For license information, please see license.txt

// Shared display helper for the marketplace platform (Noon, Shopify, Talabat) as a colored pill in the
// Marketplace Order, Marketplace Account and Sales Order lists. One color map, Frappe's built-in indicator
// colors only. Registered in `doctype_list_js` (hooks.py) and reached through
// frappe.marketplace_integrations.platform_badge. For the two doctypes this app owns, Frappe appends hook
// files after the doctype's own list script, so list scripts look the helper up when a row renders,
// never at load time.
//
// Wrapped in a function: doctype_list_js files share one scope with the other list scripts.
(function () {
	const COLORS = { Noon: "yellow", Shopify: "green", Talabat: "orange" };
	const FALLBACK_COLOR = "grey";

	// Delivery Note carrier (the native `transporter`, a Supplier name): Noon, Bosta and the manual courier
	// inDrive have a color; Uber, Own driver and any other carrier are grey.
	const CARRIER_COLORS = { Noon: "yellow", Bosta: "red", inDrive: "green" };

	function lookup(map, name) {
		return Object.prototype.hasOwnProperty.call(map, name) ? map[name] : FALLBACK_COLOR;
	}

	function color_of(platform) {
		return lookup(COLORS, platform);
	}

	function carrier_color_of(carrier) {
		return lookup(CARRIER_COLORS, carrier);
	}

	function escape(value) {
		return frappe.utils.escape_html(value);
	}

	// The pill alone; nothing for an empty value.
	function pill(platform, color = color_of(platform)) {
		if (!platform) return "";
		return `<span class="indicator-pill ${color}">${escape(__(platform))}</span>`;
	}

	function carrier_pill(carrier) {
		return pill(carrier, carrier_color_of(carrier));
	}

	// The list formatter. The pill sits in a `filterable` link, as in the native column, so a click still
	// filters the list by this value; the tooltip names the column. `df.fieldname` is the column the
	// formatter was registered for.
	function link(value, df, default_field, pill_html) {
		if (!value) return "";
		const filter = `${(df && df.fieldname) || default_field},=,${value}`;
		const title = df && df.label ? `${__(df.label)}: ${__(value)}` : __(value);
		return `<a class="filterable ellipsis" data-filter="${escape(filter)}" title="${escape(title)}">${pill_html}</a>`;
	}

	function formatter(value, df) {
		return link(value, df, "platform", pill(value));
	}

	function carrier_formatter(value, df) {
		return link(value, df, "transporter", carrier_pill(value));
	}

	const api = { COLORS, CARRIER_COLORS, color_of, carrier_color_of, pill, carrier_pill, formatter, carrier_formatter };

	if (typeof frappe !== "undefined") {
		frappe.marketplace_integrations = frappe.marketplace_integrations || {};
		frappe.marketplace_integrations.platform_badge = api;
	}
	if (typeof module !== "undefined") {
		module.exports = api;
	}
})();
