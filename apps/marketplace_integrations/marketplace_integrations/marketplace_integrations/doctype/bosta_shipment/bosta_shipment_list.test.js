const assert = require("node:assert/strict");
const test = require("node:test");

global.__ = (message) => `ar:${message}`;
global.frappe = { listview_settings: {}, utils: { escape_html: (text) => String(text).replace(/</g, "&lt;") } };

const { get_bosta_shipment_indicator, show_row_name_on_hover } = require("./bosta_shipment_list.js");

const SETTINGS = () => frappe.listview_settings["Bosta Shipment"];

test("the indicator shows the translated status with a colour and a stored-value filter", () => {
	assert.deepEqual(get_bosta_shipment_indicator({ status: "Completed" }), [
		"ar:Completed",
		"green",
		"status,=,Completed",
	]);
	assert.equal(get_bosta_shipment_indicator({ status: "Bosta Failed" })[1], "red");
	assert.equal(get_bosta_shipment_indicator({ status: "Something New" })[1], "grey");
	assert.equal(get_bosta_shipment_indicator({})[2], "status,=,Pending Bosta");
});

test("Bosta Shipment list hides the docname column; the readable title is the subject (#324)", () => {
	assert.equal(SETTINGS().hide_name_column, true);
});

test("Bosta Shipment list attaches the shared row tooltip helper once it is loaded, and survives a load failure", async () => {
	const attached = [];
	global.frappe.require = () => Promise.resolve();
	global.frappe.marketplace_integrations = { list_row_name: { attach_to_list: (lv) => attached.push(lv) } };
	const listview = { $result: {} };
	await show_row_name_on_hover(listview);
	assert.deepEqual(attached, [listview]);

	// Helper missing after the load: nothing happens, nothing throws.
	global.frappe.marketplace_integrations = {};
	await show_row_name_on_hover(listview);
	assert.equal(attached.length, 1);

	// Load failure is logged, not thrown.
	const original = console.error;
	console.error = () => {};
	global.frappe.require = () => Promise.reject(new Error("offline"));
	try {
		await show_row_name_on_hover(listview);
	} finally {
		console.error = original;
	}
});

test("the status shows only as the translated indicator: no status formatter is defined (Frappe drops that column)", () => {
	assert.equal(SETTINGS().formatters, undefined);
	assert.equal(typeof SETTINGS().get_indicator, "function");
});
