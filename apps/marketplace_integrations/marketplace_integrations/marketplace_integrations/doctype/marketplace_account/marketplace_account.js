const TALABAT_OUTLET_CLOSE_REASONS = [
	"TOO_BUSY_NO_DRIVERS",
	"TOO_BUSY_KITCHEN",
	"UPDATES_IN_MENU",
	"TECHNICAL_PROBLEM",
	"CLOSED",
	"OTHER",
	"BAD_WEATHER",
	"HOLIDAY_SPECIAL_DAY",
];

const TALABAT_OUTLET_FIELDS = [
	"outlet_status_section",
	"external_status",
	"external_status_reason",
	"external_status_until",
	"external_status_checked_on",
	"external_status_error",
];

if (typeof frappe !== "undefined") {
	frappe.ui.form.on("Marketplace Account", {
		refresh(frm) {
			configure_outlet_management(frm);
		},
		platform(frm) {
			configure_outlet_management(frm);
		},
	});
}

function configure_outlet_management(frm) {
	const is_talabat = should_show_outlet_management(frm.doc.platform);
	for (const fieldname of TALABAT_OUTLET_FIELDS) {
		frm.toggle_display(fieldname, is_talabat);
		frm.set_df_property(fieldname, "read_only", 1);
	}
	if (!is_talabat || frm.is_new()) return;

	frm.add_custom_button(__("Refresh Status"), () => refresh_outlet_status(frm));
	for (const action of outlet_actions_for_status(frm.doc.external_status)) {
		add_outlet_action(frm, action);
	}
}

function should_show_outlet_management(platform) {
	return platform === "Talabat";
}

function outlet_actions_for_status(status) {
	if (status === "OPEN") return ["close_today", "close_until"];
	if (["CLOSED", "CLOSED_TODAY", "CLOSED_UNTIL"].includes(status)) {
		return ["open", "close_today", "close_until"];
	}
	return [];
}

function add_outlet_action(frm, action) {
	if (action === "open") {
		frm.add_custom_button(__("Open Outlet"), () => set_outlet_status(frm, "OPEN"));
		return;
	}
	if (action === "close_today") {
		frm.add_custom_button(__("Close Today"), () => show_close_today_dialog(frm));
		return;
	}
	frm.add_custom_button(__("Close Until"), () => show_close_until_dialog(frm));
}

function refresh_outlet_status(frm) {
	call_outlet_api(frm, "marketplace_integrations.api.talabat_outlet.refresh_outlet_status", {});
}

function set_outlet_status(frm, status, closed_reason, closed_until) {
	call_outlet_api(frm, "marketplace_integrations.api.talabat_outlet.set_outlet_status", {
		...build_outlet_status_args(status, closed_reason, closed_until),
	});
}

function build_outlet_status_args(status, closed_reason, closed_until) {
	const args = { status };
	if (closed_reason) args.closed_reason = closed_reason;
	if (closed_until) args.closed_until = closed_until;
	return args;
}

function show_close_today_dialog(frm) {
	show_close_dialog(frm, "CLOSED_TODAY", __("Close Talabat Outlet Today"));
}

function show_close_until_dialog(frm) {
	show_close_dialog(frm, "CLOSED_UNTIL", __("Close Talabat Outlet Until"), true);
}

function show_close_dialog(frm, status, title, requires_closed_until = false) {
	const fields = [
		{
			fieldname: "closed_reason",
			fieldtype: "Select",
			label: __("Reason"),
			options: TALABAT_OUTLET_CLOSE_REASONS.join("\n"),
			reqd: 1,
		},
	];
	if (requires_closed_until) {
		fields.push({
			fieldname: "closed_until",
			fieldtype: "Datetime",
			label: __("Close Until"),
			description: __("Enter your normal local date and time. Talabat receives the server-converted UTC time."),
			reqd: 1,
		});
	}
	const dialog = new frappe.ui.Dialog({
		title,
		fields,
		primary_action_label: __("Confirm"),
		primary_action(values) {
			dialog.hide();
			set_outlet_status(frm, status, values.closed_reason, values.closed_until);
		},
	});
	dialog.show();
}

function call_outlet_api(frm, method, args) {
	frappe
		.call({
			method,
			args: { account: frm.doc.name, ...args },
			freeze: true,
			freeze_message: __("Confirming outlet status with Talabat..."),
		})
		.then(() => frm.reload_doc())
		.catch(() => {
			frappe.msgprint({
				title: __("Talabat Outlet Status"),
				message: __(
					"Talabat could not confirm the outlet status. The last confirmed status remains unchanged."
				),
				indicator: "red",
			});
		});
}

if (typeof module !== "undefined") {
	module.exports = {
		TALABAT_OUTLET_CLOSE_REASONS,
		build_outlet_status_args,
		outlet_actions_for_status,
		should_show_outlet_management,
	};
}
