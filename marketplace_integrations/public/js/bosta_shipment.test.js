const assert = require("node:assert/strict");
const test = require("node:test");

function load() {
	const calls = [];
	const handlers = {};
	const opened = [];
	global.window = { open: (...args) => opened.push(args) };
	global.__ = (message) => message;
	global.frappe = {
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

	const without_tracking = { doc: { name: "BOSTA-SHP-1", tracking_number: "" }, add_custom_button() {} };
	handlers["Bosta Shipment"].refresh(without_tracking);
	assert.equal(without_tracking.__mi_bosta_print_label_added, undefined);
});

test("the label action requests a private A4 Arabic PDF for the current shipment", async () => {
	const { handlers, calls, opened } = load();
	const buttons = [];
	const frm = {
		doc: { name: "BOSTA-SHP-1", tracking_number: "77873113" },
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
