// Copyright (c) 2026, Tamwenat El Zekrayat and contributors
// For license information, please see license.txt

// #324: Bosta Shipment's title_field is `status`, so the subject link reads as the
// shipment status instead of BOSTA-SHP-xxxxx. Tracking number and receiver are list
// columns (the cell tooltip shows the full tracking number to copy); the docname
// stays on the form and in the row tooltip.
if (typeof frappe !== "undefined") {
	frappe.listview_settings["Bosta Shipment"] = {
		formatters: {
			// The subject is a plain-text link: translate the fixed English status for display only.
			status(value) {
				return value ? __(value) : "";
			},
		},
		onload: show_row_name_on_hover,
	};
}

function show_row_name_on_hover(listview) {
	if (!listview.$result) return;
	listview.$result.on("mouseenter focusin", ".list-subject a[data-name]", function () {
		const text = this.textContent.trim();
		const name = this.dataset.name;
		if (name && text !== name) this.title = `${text} (${name})`;
	});
}

if (typeof module !== "undefined") {
	module.exports = { show_row_name_on_hover };
}
