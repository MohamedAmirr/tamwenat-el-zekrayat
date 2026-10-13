// Copyright (c) 2026, Tamwenat El Zekrayat and contributors
// For license information, please see license.txt

(function () {
	const PRINT_LABEL_METHOD =
		"marketplace_integrations.api.bosta_shipments.print_a4_arabic_label";

	function add_print_label_button(frm) {
		if (!frm.doc || !frm.doc.tracking_number || frm.__mi_bosta_print_label_added) return;
		frm.__mi_bosta_print_label_added = true;
		frm.add_custom_button(__("Print Bosta A4 Label"), () => {
			frappe
				.call({
					method: PRINT_LABEL_METHOD,
					type: "POST",
					args: { shipment: frm.doc.name },
					freeze: true,
					freeze_message: __("Preparing Bosta label..."),
				})
				.then((r) => {
					const label = r.message || {};
					if (!label.file_url) return;
					window.open(label.file_url, "_blank", "noopener");
				});
		});
	}

	if (typeof frappe !== "undefined" && frappe.ui && frappe.ui.form) {
		frappe.ui.form.on("Bosta Shipment", { refresh: add_print_label_button });
	}

	if (typeof module !== "undefined") {
		module.exports = { add_print_label_button };
	}
})();
