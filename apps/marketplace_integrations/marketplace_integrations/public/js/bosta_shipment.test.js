const assert = require("node:assert/strict");
const test = require("node:test");

function load() {
	const calls = [];
	const handlers = {};
	const opened = [];
	global.window = { open: (...args) => opened.push(args) };
	global.__ = (message) => message;
	global.frappe = {
		user: { has_role: () => true },
		ui: { form: { on: (doctype, hooks) => (handlers[doctype] = hooks) } },
		call: (options) => {
			calls.push(options);
			return Promise.resolve({ message: { file_url: "/private/files/bosta-awb.pdf" } });
		},
	};
	const path = require.resolve("./bosta_shipment.js");
	delete require.cache[path];
	const module_exports = require(path);
	return { calls, handlers, module_exports, opened };
}

test("registers a Bosta Shipment label action only when a tracking number exists", () => {
	const { handlers } = load();
	assert.equal(typeof handlers["Bosta Shipment"].refresh, "function");

	const without_tracking = { doc: { name: "BOSTA-SHP-1", tracking_number: "" }, is_new: () => false, add_custom_button() {} };
	handlers["Bosta Shipment"].refresh(without_tracking);
	assert.equal(without_tracking.buttons_added, undefined);
});

test("the label action requests a private A4 Arabic PDF for the current shipment", async () => {
	const { handlers, calls, opened } = load();
	const buttons = [];
	const frm = {
		doc: { name: "BOSTA-SHP-1", tracking_number: "77873113", status: "Completed", delivery_note: "DN" },
		is_new: () => false,
		add_custom_button: (label, action) => buttons.push({ label, action }),
	};
	handlers["Bosta Shipment"].refresh(frm);
	assert.equal(buttons.length, 1);
	assert.equal(buttons[0].label, "Print Bosta A4 Label");

	buttons[0].action();
	await new Promise((resolve) => setTimeout(resolve, 0));
	assert.equal(calls.length, 1);
	assert.equal(calls[0].method, "marketplace_integrations.api.bosta_shipments.print_a4_arabic_label");
	assert.deepEqual(calls[0].args, { shipment: "BOSTA-SHP-1" });
	assert.deepEqual(opened, [["/private/files/bosta-awb.pdf", "_blank", "noopener"]]);
});

test("the status snapshot is rendered as escaped text and Check Bosta is offered only when stuck", () => {
	const { handlers, module_exports } = load();
	global.frappe.utils = { escape_html: (value) => String(value).replace(/</g, "&lt;") };
	const df = {};
	global.frappe.meta = { docfield_map: { "Bosta Shipment": { bosta_status_snapshot: df } } };
	module_exports.render_snapshot_as_escaped_text();
	assert.equal(df.formatter('{"v":"<img src=x onerror=1>"}'), '<pre>{"v":"&lt;img src=x onerror=1>"}</pre>');
	assert.equal(df.formatter(""), "");
	assert.equal(typeof handlers["Bosta Shipment"].onload, "function");

	const buttons = [];
	const frm = (doc) => ({ doc, is_new: () => false, add_custom_button: (label) => buttons.push(label) });
	handlers["Bosta Shipment"].refresh(frm({ name: "S", status: "Completed", delivery_note: "DN" }));
	handlers["Bosta Shipment"].refresh(frm({ name: "S", status: "Bosta Failed", delivery_note: "" }));
	assert.deepEqual(buttons, ["Check Bosta"]);
});

test("buttons are re-added on every refresh and Check Bosta is hidden from non-creators", () => {
	const { handlers } = load();
	const buttons = [];
	const frm = {
		doc: { name: "S", status: "Bosta Failed", delivery_note: "", tracking_number: "T" },
		is_new: () => false,
		add_custom_button: (label) => buttons.push(label),
	};
	handlers["Bosta Shipment"].refresh(frm);
	handlers["Bosta Shipment"].refresh(frm); // e.g. after reload_doc cleared the toolbar
	assert.deepEqual(buttons, ["Print Bosta A4 Label", "Check Bosta", "Print Bosta A4 Label", "Check Bosta"]);

	buttons.length = 0;
	global.frappe.user = { has_role: () => false };
	handlers["Bosta Shipment"].refresh(frm);
	assert.deepEqual(buttons, ["Print Bosta A4 Label"]);
});

test("Delivery Note Failed shows Retry Delivery Note and an escaped red banner", () => {
	const { handlers } = load();
	global.frappe.utils = { escape_html: (value) => String(value).replace(/</g, "&lt;") };
	const buttons = [];
	const headlines = [];
	const frm = {
		doc: { name: "S", status: "Delivery Note Failed", delivery_note: "", delivery_note_error: "<b>no stock" },
		is_new: () => false,
		add_custom_button: (label) => buttons.push(label),
		dashboard: { set_headline: (...args) => headlines.push(args) },
	};
	handlers["Bosta Shipment"].refresh(frm);
	assert.deepEqual(buttons, ["Retry Delivery Note"]);
	assert.deepEqual(headlines, [["&lt;b>no stock", "red"]]);

	headlines.length = 0;
	frm.doc.status = "Bosta Failed";
	handlers["Bosta Shipment"].refresh(frm);
	assert.deepEqual(headlines, []);
});
