const assert = require("node:assert/strict");
const test = require("node:test");

global.__ = (message) => message;
global.frappe = { listview_settings: {} };

const { marketplace_account_get_indicator } = require("./marketplace_account_list.js");

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
