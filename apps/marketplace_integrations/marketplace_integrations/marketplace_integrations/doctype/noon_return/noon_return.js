frappe.ui.form.on("Noon Return", {
	refresh(frm) {
		if (frm.is_new()) return;

		if (frm.doc.sales_return) {
			frm.add_custom_button(__("Open Sales Return"), () => {
				frappe.set_route("Form", "Delivery Note", frm.doc.sales_return);
			});
		}
		if (frm.doc.credit_note) {
			frm.add_custom_button(__("Open Credit Note"), () => {
				frappe.set_route("Form", "Sales Invoice", frm.doc.credit_note);
			});
		}
		if (!frm.perm[0].write) return;

		if (frm.doc.inspection_outcome && !frm.doc.credit_note) {
			frm.add_custom_button(__("Create Credit Note"), () => {
				frappe.confirm(
					__("Create a draft Credit Note (return Sales Invoice) for one unit of this returned item?"),
					() => {
						frm.call("create_credit_note").then(() => {
							frm.reload_doc();
							frappe.show_alert({ message: __("Draft Credit Note created."), indicator: "green" });
						});
					}
				);
			});
		}

		if (frm.doc.receipt_acknowledged_on && !frm.doc.inspection_outcome) {
			frm.add_custom_button(__("Record Inspection"), () => {
				const dialog = new frappe.ui.Dialog({
					title: __("Record Inspection"),
					fields: [
						{
							fieldname: "outcome",
							fieldtype: "Select",
							label: __("Outcome"),
							options: "Sellable\nDamaged",
							reqd: 1,
							description: __(
								"Sellable drafts a Sales Return into the account warehouse. Damaged is only recorded: Noon is responsible for it."
							),
						},
					],
					primary_action_label: __("Record"),
					primary_action(values) {
						frm.call("record_inspection", { outcome: values.outcome }).then(() => {
							dialog.hide();
							frm.reload_doc();
							frappe.show_alert({ message: __("Inspection recorded."), indicator: "green" });
						});
					},
				});
				dialog.show();
			});
		}

		if (frm.doc.receipt_acknowledged_on) return;

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
