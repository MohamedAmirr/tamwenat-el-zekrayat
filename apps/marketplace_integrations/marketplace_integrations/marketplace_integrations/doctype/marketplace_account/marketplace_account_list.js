// Copyright (c) 2026, Tamwenat El Zekrayat and contributors
// For license information, please see license.txt

// #167: List View indicator for Marketplace Account - disabled accounts are
// always grey regardless of platform; a Talabat account's last confirmed
// outlet status drives its own color (red when the last status check itself
// errored, green when OPEN, orange for any CLOSED_* state); every other
// enabled account (Shopify, Noon, or a Talabat account with no confirmed
// status yet) is green.

function marketplace_account_get_indicator(doc) {
	if (!doc.enabled) return [__("Disabled"), "grey", "enabled,=,0"];

	if (doc.platform === "Talabat") {
		if (doc.external_status_error) return [__("Error"), "red", "external_status_error,is,set"];
		if (doc.external_status === "OPEN") return [__("Open"), "green", "external_status,=,OPEN"];
		if (doc.external_status && doc.external_status.startsWith("CLOSED")) {
			return [__("Closed"), "orange", "external_status,like,CLOSED%"];
		}
	}

	return [__("Enabled"), "green", "enabled,=,1"];
}

if (typeof frappe !== "undefined") {
	frappe.listview_settings["Marketplace Account"] = {
		// #324: the readable account name is the subject; the docname (ID) column is hidden.
		hide_name_column: true,
		get_indicator: marketplace_account_get_indicator,
		onload: show_row_name_on_hover,
		// The platform pill (public/js/platform_badge.js, a doctype_list_js file loaded after this one,
		// so it is looked up per row).
		formatters: { platform: format_account_platform },
	};
}

if (typeof module !== "undefined") {
	module.exports = { format_account_platform, marketplace_account_get_indicator, show_row_name_on_hover };
}

function format_account_platform(value, df) {
	const badge = frappe.marketplace_integrations && frappe.marketplace_integrations.platform_badge;
	return badge ? badge.formatter(value, df) : frappe.utils.escape_html(value || "");
}

// #324: loads the shared row tooltip helper (public/js/list_row_name.js). The tooltip is a
// convenience: if the helper cannot load, the list works the same without it. (No top-level const:
// list scripts share one global scope, so a repeated const name would break the other lists.)
function show_row_name_on_hover(listview) {
	return frappe
		.require("/assets/marketplace_integrations/js/list_row_name.js")
		.then(() => {
			const helper = frappe.marketplace_integrations && frappe.marketplace_integrations.list_row_name;
			if (helper) helper.attach_to_list(listview);
		})
		.catch((error) => console.error("List row name helper failed to load (#324).", error));
}
