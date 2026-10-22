// Copyright (c) 2026, Tamwenat El Zekrayat and contributors
// For license information, please see license.txt

// #324: Bosta Shipment's title_field is `tracking_number`, so the subject link reads as
// the tracking number (the docname while Bosta has not accepted the shipment yet).
// Status is a normal list column plus an indicator; the docname stays on the form
// and in the row tooltip.
const BOSTA_STATUS_COLORS = {
	"Pending Bosta": "orange",
	"Bosta Created": "blue",
	"Delivery Note Failed": "red",
	Completed: "green",
	"Bosta Failed": "red",
	Canceled: "grey",
};

function get_bosta_shipment_indicator(doc) {
	const status = doc.status || "Pending Bosta";
	return [__(status), BOSTA_STATUS_COLORS[status] || "grey", `status,=,${status}`];
}

if (typeof frappe !== "undefined") {
	frappe.listview_settings["Bosta Shipment"] = {
		add_fields: ["status"],
		get_indicator: get_bosta_shipment_indicator,
		formatters: {
			// Translate the fixed English status for display only (filters use the stored value).
			status(value) {
				return value ? frappe.utils.escape_html(__(value)) : "";
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
	module.exports = { get_bosta_shipment_indicator, show_row_name_on_hover };
}
