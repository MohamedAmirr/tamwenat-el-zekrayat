const assert = require("node:assert/strict");
const test = require("node:test");

global.__ = (message) => message;
let handlers;
const alerts = [];
global.frappe = {
	ui: {
		form: {
			on(_doctype, events) {
				handlers = events;
			},
		},
	},
	show_alert(alert) {
		alerts.push(alert);
	},
};

require("./noon_connection.js");

test("Test Connection is offered only after the document is saved", () => {
	const buttons = [];
	handlers.refresh({ is_new: () => true, add_custom_button: (label) => buttons.push(label) });
	assert.deepEqual(buttons, []);
	handlers.refresh({ is_new: () => false, add_custom_button: (label) => buttons.push(label) });
	assert.deepEqual(buttons, [
		"Copy Webhook URL",
		"Copy Webhook Key",
		"Refresh Webhook URL",
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
