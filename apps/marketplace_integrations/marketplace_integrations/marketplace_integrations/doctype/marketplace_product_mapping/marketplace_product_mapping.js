if (typeof frappe !== "undefined") {
	frappe.ui.form.on("Marketplace Product Mapping", {
		refresh(frm) {
			if (frm.is_new()) return;
			frm.add_custom_button(__("Push to Talabat"), () => push_to_talabat(frm));
		},
	});
}

function push_to_talabat(frm) {
	frappe
		.call({
			method: "marketplace_integrations.api.talabat_catalog.queue_mapping_catalog_sync",
			args: { mapping: frm.doc.name },
			freeze: true,
			freeze_message: __("Queuing Talabat catalog update..."),
		})
		.then(() => {
			frappe.show_alert({ message: __("Talabat catalog update queued."), indicator: "green" });
			frm.reload_doc();
		});
}

if (typeof module !== "undefined") {
	module.exports = { push_to_talabat };
}
