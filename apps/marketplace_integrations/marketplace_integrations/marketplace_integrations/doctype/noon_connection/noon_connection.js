frappe.ui.form.on("Noon Connection", {
	refresh(frm) {
		if (frm.is_new()) return;

		frm.add_custom_button(__("Test Connection"), () => {
			frm.call("test_connection").then((response) => {
				if (response.message && response.message.connected) {
					frappe.show_alert({
						message: __("Noon connection succeeded."),
						indicator: "green",
					});
				}
			});
		});
	},
});
