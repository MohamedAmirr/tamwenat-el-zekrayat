const assert = require("node:assert/strict");
const test = require("node:test");

global.__ = (message) => message;

const {
	TALABAT_OUTLET_CLOSE_REASONS,
	build_outlet_status_args,
	outlet_actions_for_status,
	should_show_outlet_management,
} = require("./marketplace_account.js");

test("outlet management is visible only for Talabat accounts", () => {
	assert.equal(should_show_outlet_management("Talabat"), true);
	assert.equal(should_show_outlet_management("Noon"), false);
});

test("Talabat outlet actions are driven only by the last confirmed status", () => {
	assert.deepEqual(outlet_actions_for_status("OPEN"), ["close_today", "close_until"]);
	assert.deepEqual(outlet_actions_for_status("CLOSED"), ["open", "close_today", "close_until"]);
	assert.deepEqual(outlet_actions_for_status("CLOSED_TODAY"), ["open", "close_today", "close_until"]);
	assert.deepEqual(outlet_actions_for_status("CLOSED_UNTIL"), ["open", "close_today", "close_until"]);
	assert.deepEqual(outlet_actions_for_status("CHECKIN"), []);
	assert.deepEqual(outlet_actions_for_status(undefined), []);
});

test("Talabat outlet dialogs expose only documented close reasons", () => {
	assert.deepEqual(TALABAT_OUTLET_CLOSE_REASONS, [
		"TOO_BUSY_NO_DRIVERS",
		"TOO_BUSY_KITCHEN",
		"UPDATES_IN_MENU",
		"TECHNICAL_PROBLEM",
		"CLOSED",
		"OTHER",
		"BAD_WEATHER",
		"HOLIDAY_SPECIAL_DAY",
	]);
});

test("Open sends no close fields and Close Until preserves the local DateTime value", () => {
	assert.deepEqual(build_outlet_status_args("OPEN"), { status: "OPEN" });
	assert.deepEqual(build_outlet_status_args("CLOSED_TODAY", "OTHER"), {
		status: "CLOSED_TODAY",
		closed_reason: "OTHER",
	});
	assert.deepEqual(build_outlet_status_args("CLOSED_UNTIL", "OTHER", "2026-09-02 15:30:00"), {
		status: "CLOSED_UNTIL",
		closed_reason: "OTHER",
		closed_until: "2026-09-02 15:30:00",
	});
});
