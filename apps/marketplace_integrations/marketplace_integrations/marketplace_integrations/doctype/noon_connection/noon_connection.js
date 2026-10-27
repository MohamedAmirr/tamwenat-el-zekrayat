const MAX_NOON_DESTINATIONS = 5;
const REQUIRED_CREDENTIAL_KEYS = ["key_id", "private_key", "project_code"];

// The file is read in the browser only. It is never uploaded, stored or logged.
function parse_credentials_text(text) {
	let data;
	try {
		data = JSON.parse(text);
	} catch (error) {
		return { error: __("The file is not valid JSON.") };
	}
	if (!data || typeof data !== "object" || Array.isArray(data)) {
		return { error: __("The file must contain a JSON object.") };
	}
	const missing = REQUIRED_CREDENTIAL_KEYS.filter(
		(key) => typeof data[key] !== "string" || !data[key].trim()
	);
	if (missing.length) {
		return {
			error: __("The credentials file is missing these keys or they are empty: {0}", [missing.join(", ")]),
		};
	}
	return {
		credentials: { ...data, project_code: data.project_code.trim(), key_id: data.key_id.trim() },
	};
}

function apply_credentials(frm, credentials) {
	frm.set_value("project_code", credentials.project_code);
	frm.set_value("key_id", credentials.key_id);
	frm.set_value("private_key", credentials.private_key);
	const channel_identifier = credentials.channel_identifier;
	if (!frm.doc.connection_name && typeof channel_identifier === "string" && channel_identifier.trim()) {
		frm.set_value("connection_name", channel_identifier.trim());
	}
	frappe.show_alert({
		message: __("Credentials imported. Review them, then press Save."),
		indicator: "green",
	});
}

function import_credentials_text(frm, text) {
	const parsed = parse_credentials_text(text);
	if (parsed.error) {
		frappe.msgprint({ message: frappe.utils.escape_html(parsed.error), indicator: "red" });
		return;
	}
	const credentials = parsed.credentials;
	if (!credentials.private_key.includes("-----BEGIN")) {
		frappe.show_alert({
			message: __("The private key does not look like a PEM key (no {0} line). Check the file before saving.", [
				"-----BEGIN",
			]),
			indicator: "orange",
		});
	}
	const doc = frm.doc || {};
	const differs =
		(doc.project_code && doc.project_code !== credentials.project_code) ||
		(doc.key_id && doc.key_id !== credentials.key_id);
	if (differs) {
		frappe.confirm(
			__("This form already has a different Project Code or Key ID. Overwrite them with the values from the file?"),
			() => apply_credentials(frm, credentials)
		);
		return;
	}
	apply_credentials(frm, credentials);
}

function pick_credentials_file(frm) {
	const input = document.createElement("input");
	input.type = "file";
	input.accept = ".json,application/json";
	input.onchange = () => {
		const file = input.files && input.files[0];
		if (!file) return;
		const reader = new FileReader();
		reader.onload = () => import_credentials_text(frm, String(reader.result || ""));
		reader.onerror = () =>
			frappe.msgprint({ message: __("The file could not be read."), indicator: "red" });
		reader.readAsText(file);
	};
	input.click();
}

function show_webhook_result(frm, result) {
	result = result || {};
	frm.set_value("webhook_status", result.status || "");
	frm.set_value("other_noon_destinations", result.other_destinations || 0);
	frm.set_value("last_webhook_event_received_on", result.last_event_received_on || null);
	if (result.error) {
		frappe.msgprint(frappe.utils.escape_html(result.error));
		return;
	}
	frappe.show_alert({
		message: __("Webhook status: {0}", [frappe.utils.escape_html(result.status || "")]),
		indicator: result.status === "Active" ? "green" : "orange",
	});
	frm.reload_doc();
}

function call_webhook_method(frm, method, args) {
	return frm.call(method, args).then((response) => show_webhook_result(frm, response.message));
}

function add_webhook_destination_buttons(frm) {
	const doc = frm.doc || {};
	const group = __("Noon Webhook");

	if (doc.webhook_status === "Not registered") {
		frm.add_custom_button(
			__("Register Webhook in Noon"),
			() => {
				if ((doc.other_noon_destinations || 0) >= MAX_NOON_DESTINATIONS) {
					frappe.msgprint(
						__("Noon allows {0} destinations and this project already has that many.", [
							MAX_NOON_DESTINATIONS,
						])
					);
					return;
				}
				frappe.confirm(
					__("Create a Noon destination for {0} that sends the {1} header (the key is not shown) and subscribe it to FBPI::ORDER_SYNC?", [
						frappe.utils.escape_html(doc.webhook_url || ""),
						"X-Api-Key",
					]),
					() => call_webhook_method(frm, "register_webhook")
				);
			},
			group
		);
	}
	if (doc.webhook_status === "Registered, not subscribed") {
		frm.add_custom_button(
			__("Subscribe to Order Sync"),
			() => call_webhook_method(frm, "subscribe_webhook"),
			group
		);
	}
	if (doc.noon_destination_code) {
		frm.add_custom_button(
			__("Update Noon Destination"),
			() => {
				frappe.prompt(
					[
						{
							fieldname: "send_key",
							fieldtype: "Check",
							label: __("Also send the current webhook key"),
						},
					],
					(values) => call_webhook_method(frm, "update_webhook_destination", { send_key: values.send_key ? 1 : 0 }),
					__("Send the current webhook URL to Noon"),
					__("Update")
				);
			},
			group
		);
		if (doc.webhook_status === "Active") {
			frm.add_custom_button(
				__("Deactivate Destination"),
				() => call_webhook_method(frm, "set_webhook_active", { active: 0 }),
				group
			);
		}
		if (doc.webhook_status === "Inactive") {
			frm.add_custom_button(
				__("Activate Destination"),
				() => call_webhook_method(frm, "set_webhook_active", { active: 1 }),
				group
			);
		}
	}
}

frappe.ui.form.on("Noon Connection", {
	refresh(frm) {
		if (frm.perm && frm.perm[0] && frm.perm[0].write) {
			frm.add_custom_button(__("Import Credentials File"), () => pick_credentials_file(frm));
		}
		if (frm.is_new()) return;

		frm.add_custom_button(__("Copy Webhook URL"), () => {
			if (!frm.doc.webhook_url) {
				frappe.msgprint(__("Save the connection first to generate the webhook URL."));
				return;
			}
			frappe.utils.copy_to_clipboard(frm.doc.webhook_url);
			frappe.show_alert({ message: __("Webhook URL copied."), indicator: "green" });
		});

		frm.add_custom_button(__("Copy Webhook Key"), () => {
			frm.call("get_webhook_secret_for_copy").then((response) => {
				if (!response.message.webhook_secret) {
					frappe.msgprint(__("Save the connection first to generate the webhook key."));
					return;
				}
				frappe.utils.copy_to_clipboard(response.message.webhook_secret);
				frappe.show_alert({ message: __("Webhook key copied."), indicator: "green" });
			});
		});

		frm.add_custom_button(__("Refresh Webhook URL"), () => {
			if (frm.is_dirty()) {
				frappe.msgprint(__("Save your changes first, then refresh the webhook URL."));
				return;
			}
			frm.call("refresh_webhook_url").then((response) => {
				frm.set_value("webhook_url", response.message.webhook_url);
				frappe.show_alert({
					message: __("Webhook URL refreshed. Update it in Noon's Partner Center."),
					indicator: "green",
				});
			});
		});

		frm.add_custom_button(__("Refresh Webhook Status"), () => {
			frm.call("refresh_webhook_status").then((response) => show_webhook_result(frm, response.message));
		});

		add_webhook_destination_buttons(frm);

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

frappe.ui.form.on("Noon Destination", {
	toggle_active(frm, cdt, cdn) {
		const row = locals[cdt][cdn];
		const activate = !row.is_active;
		frappe.confirm(
			activate
				? __("Activate destination {0}?", [frappe.utils.escape_html(row.destination_name || row.destination_code)])
				: __("Deactivate destination {0}? Noon stops sending events to it.", [
						frappe.utils.escape_html(row.destination_name || row.destination_code),
				  ]),
			() => call_webhook_method(frm, "set_destination_active", { destination_code: row.destination_code, active: activate ? 1 : 0 })
		);
	},
});

if (typeof module !== "undefined" && module.exports) {
	module.exports = { parse_credentials_text, apply_credentials, import_credentials_text };
}
