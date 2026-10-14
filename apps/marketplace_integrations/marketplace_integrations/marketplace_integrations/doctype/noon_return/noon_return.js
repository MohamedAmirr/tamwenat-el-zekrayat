frappe.ui.form.on("Noon Return", {
	refresh(frm) {
		if (frm.is_new() || frm.doc.receipt_acknowledged_on || !frm.perm[0].write) return;

		frm.add_custom_button(__("Acknowledge Return Received"), () => {
			const dialog = new frappe.ui.Dialog({
				title: __("Acknowledge Return Received"),
				fields: [
					{
						fieldname: "receipt_note",
						fieldtype: "Small Text",
						label: __("Internal Note"),
					},
				],
				primary_action_label: __("Acknowledge"),
				primary_action(values) {
					frm.call("acknowledge_receipt", { receipt_note: values.receipt_note }).then((response) => {
						dialog.hide();
						frm.reload_doc();
						frappe.show_alert({
							message: response.message.already_acknowledged
								? __("Return receipt was already acknowledged.")
								: __("Return receipt acknowledged internally."),
							indicator: "green",
						});
					});
				},
			});
			dialog.show();
		});
	},
});
