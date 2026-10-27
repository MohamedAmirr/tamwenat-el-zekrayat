const assert = require("node:assert/strict");
const test = require("node:test");

global.__ = (message, args) => (args || []).reduce((text, value, index) => text.replace(`{${index}}`, value), message);
let handlers;
let handlers_destination;
const alerts = [];
global.frappe = {
	ui: {
		form: {
			on(doctype, events) {
				if (doctype === "Noon Destination") handlers_destination = events;
				else handlers = events;
			},
		},
	},
	show_alert(alert) {
		alerts.push(alert);
	},
};

const { import_credentials_text } = require("./noon_connection.js");

test("Test Connection is offered only after the document is saved", () => {
	const buttons = [];
	handlers.refresh({ is_new: () => true, add_custom_button: (label) => buttons.push(label) });
	assert.deepEqual(buttons, ["Import Credentials File"]);
	buttons.length = 0;
	handlers.refresh({ is_new: () => false, doc: {}, add_custom_button: (label) => buttons.push(label) });
	assert.deepEqual(buttons, [
		"Import Credentials File",
		"Copy Webhook URL",
		"Copy Webhook Key",
		"Refresh Webhook URL",
		"Refresh Webhook Status",
		"Test Connection",
	]);
});

test("Test Connection calls the server method and shows only a success indicator", async () => {
	alerts.length = 0;
	let button;
	let method;
	const frm = {
		is_new: () => false,
		add_custom_button(_label, action) {
			button = action;
		},
		call(name) {
			method = name;
			return Promise.resolve({ message: { connected: true } });
		},
	};
	handlers.refresh(frm);
	button();
	await Promise.resolve();
	assert.equal(method, "test_connection");
	assert.deepEqual(alerts, [{ message: "Noon connection succeeded.", indicator: "green" }]);
});

test("Copy Webhook URL with empty webhook_url shows msgprint and does not copy", () => {
	alerts.length = 0;
	const buttons = [];
	let msgprinted = false;
	const frm = {
		is_new: () => false,
		doc: { webhook_url: "" },
		add_custom_button(label, action) {
			buttons.push({ label, action });
		},
	};
	let clipboardValue = "";
	global.frappe.msgprint = (message) => {
		msgprinted = true;
	};
	global.frappe.utils = {
		copy_to_clipboard(value) {
			clipboardValue = value;
		},
	};
	handlers.refresh(frm);
	const copyUrlButton = buttons.find((b) => b.label.includes("Webhook URL"));
	copyUrlButton.action();

	assert.equal(msgprinted, true);
	assert.equal(clipboardValue, "");
	assert.deepEqual(alerts, []);
});

test("Copy Webhook URL copies the URL from the document and shows a green alert", () => {
	alerts.length = 0;
	const buttons = [];
	const frm = {
		is_new: () => false,
		doc: { webhook_url: "https://example.com/api/method/foo" },
		add_custom_button(label, action) {
			buttons.push({ label, action });
		},
	};
	let clipboardValue = "";
	global.frappe.utils = {
		copy_to_clipboard(value) {
			clipboardValue = value;
		},
	};
	handlers.refresh(frm);
	const copyUrlButton = buttons.find((b) => b.label.includes("Webhook URL"));
	copyUrlButton.action();

	assert.equal(clipboardValue, "https://example.com/api/method/foo");
	assert.deepEqual(alerts, [{ message: "Webhook URL copied.", indicator: "green" }]);
});

test("Copy Webhook Key with empty returned secret shows msgprint and does not copy", async () => {
	alerts.length = 0;
	const buttons = [];
	let msgprinted = false;
	const frm = {
		is_new: () => false,
		doc: { webhook_url: "https://example.com/api/method/foo" },
		add_custom_button(label, action) {
			buttons.push({ label, action });
		},
		call(name) {
			return Promise.resolve({ message: { webhook_secret: "" } });
		},
	};
	let clipboardValue = "";
	global.frappe.msgprint = (message) => {
		msgprinted = true;
	};
	global.frappe.utils = {
		copy_to_clipboard(value) {
			clipboardValue = value;
		},
	};
	handlers.refresh(frm);
	const copyKeyButton = buttons.find((b) => b.label.includes("Webhook Key"));
	copyKeyButton.action();
	await Promise.resolve();

	assert.equal(msgprinted, true);
	assert.equal(clipboardValue, "");
	assert.deepEqual(alerts, []);
});

test("Copy Webhook Key calls get_webhook_secret_for_copy and copies the secret", async () => {
	alerts.length = 0;
	const buttons = [];
	let callMethod;
	const frm = {
		is_new: () => false,
		doc: { webhook_url: "https://example.com/api/method/foo" },
		add_custom_button(label, action) {
			buttons.push({ label, action });
		},
		call(name) {
			callMethod = name;
			return Promise.resolve({ message: { webhook_secret: "secret-12345" } });
		},
	};
	let clipboardValue = "";
	global.frappe.utils = {
		copy_to_clipboard(value) {
			clipboardValue = value;
		},
	};
	handlers.refresh(frm);
	const copyKeyButton = buttons.find((b) => b.label.includes("Webhook Key"));
	copyKeyButton.action();
	await Promise.resolve();

	assert.equal(callMethod, "get_webhook_secret_for_copy");
	assert.equal(clipboardValue, "secret-12345");
	assert.deepEqual(alerts, [{ message: "Webhook key copied.", indicator: "green" }]);
});

test("Refresh Webhook URL calls refresh_webhook_url and updates the form field", async () => {
	alerts.length = 0;
	const buttons = [];
	let callMethod;
	let setValueCall = { field: null, value: null };
	const frm = {
		is_new: () => false,
		is_dirty: () => false,
		doc: { webhook_url: "https://old.example.com/api/method/foo" },
		add_custom_button(label, action) {
			buttons.push({ label, action });
		},
		call(name) {
			callMethod = name;
			return Promise.resolve({
				message: { webhook_url: "https://new.example.com/api/method/foo" },
			});
		},
		set_value(field, value) {
			setValueCall = { field, value };
		},
	};
	handlers.refresh(frm);
	const refreshUrlButton = buttons.find((b) => b.label.includes("Refresh Webhook URL"));
	refreshUrlButton.action();
	await Promise.resolve();

	assert.equal(callMethod, "refresh_webhook_url");
	assert.equal(setValueCall.field, "webhook_url");
	assert.equal(setValueCall.value, "https://new.example.com/api/method/foo");
	assert.equal(alerts[0].message, "Webhook URL refreshed. Update it in Noon's Partner Center.");
	assert.equal(alerts[0].indicator, "green");
});

test("Refresh Webhook URL with unsaved changes shows msgprint and does not call the server", () => {
	alerts.length = 0;
	const buttons = [];
	let called = false;
	let message = "";
	global.frappe.msgprint = (text) => {
		message = text;
	};
	const frm = {
		is_new: () => false,
		is_dirty: () => true,
		doc: { webhook_url: "https://old.example.com/api/method/foo" },
		add_custom_button(label, action) {
			buttons.push({ label, action });
		},
		call() {
			called = true;
			return Promise.resolve({ message: {} });
		},
	};
	handlers.refresh(frm);
	buttons.find((b) => b.label.includes("Refresh Webhook URL")).action();

	assert.equal(message, "Save your changes first, then refresh the webhook URL.");
	assert.equal(called, false);
	assert.deepEqual(alerts, []);
});

function destination_buttons(doc) {
	const buttons = [];
	handlers.refresh({
		is_new: () => false,
		doc,
		add_custom_button: (label) => buttons.push(label),
	});
	return buttons.filter((label) => !/^(Copy|Refresh|Test|Import)/.test(label));
}

test("Destination actions follow the cached webhook status", () => {
	assert.deepEqual(destination_buttons({ webhook_status: "Not registered" }), ["Register Webhook in Noon"]);
	assert.deepEqual(destination_buttons({ webhook_status: "Registered, not subscribed", noon_destination_code: "d" }), [
		"Subscribe to Order Sync",
		"Update Noon Destination",
	]);
	assert.deepEqual(destination_buttons({ webhook_status: "Active", noon_destination_code: "d" }), [
		"Update Noon Destination",
		"Deactivate Destination",
	]);
	assert.deepEqual(destination_buttons({ webhook_status: "Inactive", noon_destination_code: "d" }), [
		"Update Noon Destination",
		"Activate Destination",
	]);
});

test("Register is blocked with an explanation when Noon already has five destinations", () => {
	let register;
	let message;
	global.frappe.msgprint = (text) => {
		message = text;
	};
	handlers.refresh({
		is_new: () => false,
		doc: { webhook_status: "Not registered", other_noon_destinations: 5 },
		add_custom_button(label, action) {
			if (label === "Register Webhook in Noon") register = action;
		},
	});
	register();
	assert.match(message, /Noon allows .* destinations/);
});

test("Destination row button asks for confirmation then toggles the listed destination", () => {
	let confirmed;
	let args;
	global.frappe.utils = { ...global.frappe.utils, escape_html: (text) => text };
	global.frappe.confirm = (_message, action) => {
		confirmed = action;
	};
	global.locals = { "Noon Destination": { r1: { destination_code: "d1", destination_name: "n", is_active: 1 } } };
	handlers_destination.toggle_active(
		{ set_value() {}, reload_doc() {}, call: (_method, call_args) => ((args = call_args), Promise.resolve({ message: {} })) },
		"Noon Destination",
		"r1"
	);
	confirmed();
	assert.deepEqual(args, { destination_code: "d1", active: 0 });
});

// Credential import. All values are fake.
const FAKE_PEM = "-----BEGIN PRIVATE KEY-----\nFAKE\n-----END PRIVATE KEY-----";
function fake_file(overrides) {
	return JSON.stringify({
		key_id: "test-key",
		private_key: FAKE_PEM,
		project_code: "p000000",
		channel_identifier: "user@p000000.idp.noon.partners",
		type: "service_account",
		issued_at: "2026-01-01T00:00:00Z",
		...overrides,
	});
}

function import_setup(doc) {
	const set = {};
	const out = { set, msgs: [], confirms: [], alerts: [] };
	global.frappe.utils = { escape_html: (v) => v };
	global.frappe.msgprint = (m) => out.msgs.push(m);
	global.frappe.confirm = (message, yes) => out.confirms.push({ message, yes });
	global.frappe.show_alert = (a) => out.alerts.push(a);
	out.frm = { doc, set_value: (k, v) => (set[k] = v) };
	return out;
}

test("credentials import fills the three fields and prefills an empty Connection Name", () => {
	const t = import_setup({ environment: "Production" });
	import_credentials_text(t.frm, fake_file());
	assert.deepEqual(t.set, {
		project_code: "p000000",
		key_id: "test-key",
		private_key: FAKE_PEM,
		connection_name: "user@p000000.idp.noon.partners",
	});
	assert.equal(t.alerts.at(-1).indicator, "green");
	assert.deepEqual(t.msgs, []);
});

test("credentials import keeps a non-empty Connection Name and ignores environment, type and issued_at", () => {
	const t = import_setup({ connection_name: "My Noon", environment: "Static Sandbox" });
	import_credentials_text(t.frm, fake_file());
	assert.equal(t.set.connection_name, undefined);
	assert.deepEqual(Object.keys(t.set).sort(), ["key_id", "private_key", "project_code"]);
});

test("credentials import errors change nothing", () => {
	for (const text of ["not json", "[1]", "null", "42", fake_file({ key_id: "" }), fake_file({ private_key: undefined, project_code: 5 })]) {
		const t = import_setup({});
		import_credentials_text(t.frm, text);
		assert.deepEqual(t.set, {});
		assert.equal(t.msgs.length, 1);
		assert.equal(t.msgs[0].indicator, "red");
		assert.deepEqual(t.alerts, []);
	}
	const t = import_setup({});
	import_credentials_text(t.frm, fake_file({ key_id: "", private_key: undefined }));
	assert.match(t.msgs[0].message, /key_id, private_key/);
	assert.doesNotMatch(t.msgs[0].message, /project_code/);
});

test("a non-PEM private key warns but still imports", () => {
	const t = import_setup({});
	import_credentials_text(t.frm, fake_file({ private_key: "FAKE" }));
	assert.equal(t.set.private_key, "FAKE");
	assert.equal(t.alerts[0].indicator, "orange");
	assert.equal(t.alerts.at(-1).indicator, "green");
});

test("different existing Project Code or Key ID asks for confirmation first", () => {
	const t = import_setup({ project_code: "p111111", key_id: "test-key" });
	import_credentials_text(t.frm, fake_file());
	assert.deepEqual(t.set, {});
	assert.equal(t.confirms.length, 1);
	t.confirms[0].yes();
	assert.equal(t.set.project_code, "p000000");
});

test("identical existing values do not ask for confirmation", () => {
	const t = import_setup({ project_code: "p000000", key_id: "test-key" });
	import_credentials_text(t.frm, fake_file());
	assert.equal(t.confirms.length, 0);
	assert.equal(t.set.key_id, "test-key");
});
