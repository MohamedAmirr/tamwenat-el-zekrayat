// The three inspection outcomes (stored values; "Sellable" is shown as "Restock"). The Noon Return list
// dialog (noon_return_list.js) offers the same three; keep them in step with INSPECTION_OUTCOMES in
// noon_return.py.
function noon_return_form_outcome_options() {
	return [
		{ value: "Sellable", label: __("Restock") },
		{ value: "Damaged", label: __("Damaged") },
		{ value: "Damaged, Noon paid", label: __("Damaged, Noon paid") },
	];
}

function is_cancelled(doctype, name) {
	return frappe.db.get_value(doctype, name, "docstatus").then((response) => {
		return Boolean(response.message) && response.message.docstatus === 2;
	});
}

function add_credit_note_button(frm) {
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
			add_credit_note_button(frm);
		} else if (frm.doc.credit_note) {
			// A cancelled Credit Note can be replaced; the server allows it.
			const credit_note = frm.doc.credit_note;
			is_cancelled("Sales Invoice", credit_note).then((cancelled) => {
				// Skip if the user moved to another record while the lookup ran.
				if (cancelled && frm.doc.credit_note === credit_note) add_credit_note_button(frm);
			});
		}
		// Every outcome drafts a Sales Return (Restock into the account warehouse, the damaged
		// ones into the damaged warehouse), so a cancelled one can be created again for any of them.
		// A row recorded before #549 (Damaged, no Sales Return) has none yet: it gets the button too.
		if (frm.doc.inspection_outcome) {
			const sales_return = frm.doc.sales_return;
			const outcome = frm.doc.inspection_outcome;
			const add_button = () => {
				frm.add_custom_button(__("Create Sales Return"), () => {
					frm.call("record_inspection", { outcome }).then(() => {
						frm.reload_doc();
						frappe.show_alert({ message: __("Draft Sales Return created."), indicator: "green" });
					});
				});
			};
			if (!sales_return) {
				add_button();
			} else {
				is_cancelled("Delivery Note", sales_return).then((cancelled) => {
					if (!cancelled || frm.doc.sales_return !== sales_return) return;
					add_button();
				});
			}
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
							options: noon_return_form_outcome_options(),
							reqd: 1,
							description: __(
								"Restock drafts a Sales Return into the account warehouse. Damaged and Damaged, Noon paid come back to us: a Sales Return is drafted into the damaged warehouse, so they are not restocked."
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
