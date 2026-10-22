// Copyright (c) 2026, Tamwenat El Zekrayat and contributors
// For license information, please see license.txt

(function () {
	const PRINT_LABEL_METHOD =
		"marketplace_integrations.api.bosta_shipments.print_a4_arabic_label";

	const CHECK_METHOD = "marketplace_integrations.api.bosta_shipments.check_shipment";
	const CREATOR_ROLES = ["Sales Manager", "Stock User", "System Manager"];
	const STUCK_STATUSES = ["Pending Bosta", "Bosta Failed", "Delivery Note Failed", "Bosta Created"];

	// bosta_status_snapshot is remote text in a read-only JSON field, which the framework
	// renders as raw HTML. Set once at script load and re-asserted in onload (see
	// docs/frappe-conventions.md, JSON-field escaping); Report View is not covered.
	function render_snapshot_as_escaped_text() {
		const docfield_map =
			frappe.meta && frappe.meta.docfield_map && frappe.meta.docfield_map["Bosta Shipment"];
		const df = docfield_map && docfield_map.bosta_status_snapshot;
		if (!df) return;
		df.formatter = (value) => (value ? `<pre>${frappe.utils.escape_html(value)}</pre>` : "");
	}

	function add_check_bosta_button(frm) {
		// No "already added" flag: the form clears custom buttons on every refresh.
		if (!frm.doc || frm.is_new() || !(frappe.user && frappe.user.has_role(CREATOR_ROLES))) return;
		if (!STUCK_STATUSES.includes(frm.doc.status) || frm.doc.delivery_note) return;
		const retry_dn = frm.doc.status === "Delivery Note Failed";
		frm.add_custom_button(retry_dn ? __("Retry Delivery Note") : __("Check Bosta"), () => {
			frappe
				.call({
					method: CHECK_METHOD,
					type: "POST",
					args: { shipment: frm.doc.name },
					freeze: true,
					freeze_message: retry_dn ? __("Creating the Delivery Note...") : __("Checking Bosta..."),
				})
				.then((r) => {
					const result = r.message || {};
					frappe.show_alert({
						message: result.delivery_note
							? __("Delivery Note created.")
							: result.found === false
								? __("Bosta has no such delivery. A retry is allowed.")
								: __("Bosta delivery linked."),
						indicator: result.found === false ? "orange" : "green",
					});
				})
				.finally(() => frm.reload_doc());
		});
	}

	function show_delivery_note_error(frm) {
		if (!frm.doc || frm.doc.status !== "Delivery Note Failed" || !frm.doc.delivery_note_error) return;
		frm.dashboard.set_headline(frappe.utils.escape_html(frm.doc.delivery_note_error), "red");
	}

	function add_print_label_button(frm) {
		if (!frm.doc || !frm.doc.tracking_number) return;
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
		render_snapshot_as_escaped_text();
		frappe.ui.form.on("Bosta Shipment", {
			onload: render_snapshot_as_escaped_text,
			refresh(frm) {
				add_print_label_button(frm);
				add_check_bosta_button(frm);
				show_delivery_note_error(frm);
			},
		});
	}

	if (typeof module !== "undefined") {
		module.exports = { add_print_label_button, add_check_bosta_button, show_delivery_note_error, render_snapshot_as_escaped_text };
	}
})();
