// Copyright (c) 2026, Tamwenat El Zekrayat and contributors
// For license information, please see license.txt

// #324: Bosta Shipment's title_field is `tracking_number`, so the subject link reads as
// the tracking number (list/link display shows the docname while Bosta has not accepted the
// shipment yet; Document.get_title() itself returns ''). The status shows as the translated,
// coloured indicator pill: defining get_indicator makes Frappe drop the status field column.
// The docname (ID) column is hidden; the docname stays on the form, in search and in the row tooltip.
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
		hide_name_column: true,
		add_fields: ["status"],
		get_indicator: get_bosta_shipment_indicator,
		onload: show_row_name_on_hover,
	};
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

if (typeof module !== "undefined") {
	module.exports = { get_bosta_shipment_indicator, show_row_name_on_hover };
}
