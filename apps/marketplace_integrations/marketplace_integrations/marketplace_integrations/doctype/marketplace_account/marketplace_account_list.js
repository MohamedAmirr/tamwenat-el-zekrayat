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
		get_indicator: marketplace_account_get_indicator,
		onload: show_row_name_on_hover,
	};
}

if (typeof module !== "undefined") {
	module.exports = { marketplace_account_get_indicator, show_row_name_on_hover };
}

// #324: the subject link shows a readable title; keep the docname reachable as the
// row tooltip (set lazily on hover/focus, so no per-row work on render).
function show_row_name_on_hover(listview) {
	if (!listview.$result) return;
	listview.$result.on("mouseenter focusin", ".list-subject a[data-name]", function () {
		const text = this.textContent.trim();
		const name = this.dataset.name;
		if (name && text !== name) this.title = `${text} (${name})`;
	});
}
