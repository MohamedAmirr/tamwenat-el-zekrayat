const assert = require("node:assert/strict");
const test = require("node:test");

global.__ = (message) => `ar:${message}`;
global.frappe = { listview_settings: {}, utils: { escape_html: (text) => String(text).replace(/</g, "&lt;") } };

const { get_bosta_shipment_indicator, show_row_name_on_hover } = require("./bosta_shipment_list.js");

const SETTINGS = () => frappe.listview_settings["Bosta Shipment"];

test("the status column is translated for display and empty-safe", () => {
	assert.equal(SETTINGS().formatters.status("Bosta Created"), "ar:Bosta Created");
	assert.equal(SETTINGS().formatters.status(""), "");
});

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

test("the row tooltip adds the docname after the tracking number", () => {
	let handler;
	show_row_name_on_hover({ $result: { on: (_events, _selector, fn) => (handler = fn) } });
	const link = { textContent: "TRK123", dataset: { name: "BOSTA-SHP-00001" }, title: "" };
	handler.call(link);
	assert.equal(link.title, "TRK123 (BOSTA-SHP-00001)");
});

test("the tooltip is skipped when the subject already is the docname", () => {
	let handler;
	show_row_name_on_hover({ $result: { on: (_events, _selector, fn) => (handler = fn) } });
	const link = { textContent: "BOSTA-SHP-00001", dataset: { name: "BOSTA-SHP-00001" }, title: "" };
	handler.call(link);
	assert.equal(link.title, "");
});
