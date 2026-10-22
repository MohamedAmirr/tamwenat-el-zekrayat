const assert = require("node:assert/strict");
const test = require("node:test");

global.__ = (message) => `ar:${message}`;
global.frappe = { listview_settings: {} };

const { show_row_name_on_hover } = require("./bosta_shipment_list.js");

const SETTINGS = () => frappe.listview_settings["Bosta Shipment"];

test("the status subject is translated for display and empty-safe", () => {
	assert.equal(SETTINGS().formatters.status("Bosta Created"), "ar:Bosta Created");
	assert.equal(SETTINGS().formatters.status(""), "");
});

test("the row tooltip adds the docname after the status text", () => {
	let handler;
	show_row_name_on_hover({ $result: { on: (_events, _selector, fn) => (handler = fn) } });
	const link = { textContent: "Bosta Created", dataset: { name: "BOSTA-SHP-00001" }, title: "" };
	handler.call(link);
	assert.equal(link.title, "Bosta Created (BOSTA-SHP-00001)");
});
