const assert = require("node:assert/strict");
const test = require("node:test");

global.__ = (message) => message;
global.frappe = { listview_settings: {} };

const { marketplace_account_get_indicator, show_row_name_on_hover } = require("./marketplace_account_list.js");

test("a disabled account is always grey, regardless of platform or status", () => {
	for (const platform of ["Talabat", "Shopify", "Noon"]) {
		const [, color] = marketplace_account_get_indicator({
			enabled: 0,
			platform,
			external_status: "OPEN",
		});
		assert.equal(color, "grey");
	}
});

test("a Talabat account with a confirmed status error is red", () => {
	const [label, color] = marketplace_account_get_indicator({
		enabled: 1,
		platform: "Talabat",
		external_status_error: "Could not confirm status",
	});
	assert.equal(color, "red");
	assert.equal(label, "Error");
});

test("a Talabat account with OPEN status is green", () => {
	const [, color] = marketplace_account_get_indicator({
		enabled: 1,
		platform: "Talabat",
		external_status: "OPEN",
	});
	assert.equal(color, "green");
});

test("a Talabat account with any CLOSED_* status is orange", () => {
	for (const status of ["CLOSED", "CLOSED_TODAY", "CLOSED_UNTIL"]) {
		const [, color] = marketplace_account_get_indicator({
			enabled: 1,
			platform: "Talabat",
			external_status: status,
		});
		assert.equal(color, "orange");
	}
});

test("a Talabat account with no confirmed status yet is green, like other enabled accounts", () => {
	const [, color] = marketplace_account_get_indicator({ enabled: 1, platform: "Talabat" });
	assert.equal(color, "green");
});

test("an enabled Shopify or Noon account is green", () => {
	for (const platform of ["Shopify", "Noon"]) {
		const [, color] = marketplace_account_get_indicator({ enabled: 1, platform });
		assert.equal(color, "green");
	}
});

test("the error check takes priority over a stale OPEN/CLOSED status on the same Talabat account", () => {
	const [, color] = marketplace_account_get_indicator({
		enabled: 1,
		platform: "Talabat",
		external_status: "OPEN",
		external_status_error: "Could not confirm status",
	});
	assert.equal(color, "red");
});

test("Marketplace Account list hides the docname column; the readable title is the subject (#324)", () => {
	assert.equal(frappe.listview_settings["Marketplace Account"].hide_name_column, true);
});

test("Marketplace Account list attaches the shared row tooltip helper once it is loaded, and survives a load failure", async () => {
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
