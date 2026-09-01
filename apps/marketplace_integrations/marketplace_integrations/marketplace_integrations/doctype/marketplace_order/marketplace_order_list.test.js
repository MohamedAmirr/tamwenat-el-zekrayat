const assert = require("node:assert/strict");
const test = require("node:test");

global.__ = (message, values = []) =>
	values.reduce(
		(formatted, value, index) => formatted.replace(`{${index}}`, String(value)),
		message
	);

const { build_utc_range, format_import_result } = require("./marketplace_order_list.js");

test("Talabat retrieval result summarizes successful imports", () => {
	assert.deepEqual(format_import_result({ count: 3, errors: [] }), {
		title: "Talabat Retrieval Complete",
		indicator: "green",
		message: "Retrieved 3 orders.",
	});
});

test("Talabat retrieval result does not render imported error text", () => {
	assert.deepEqual(format_import_result({ count: 2, errors: [{ error: "<unsafe>" }] }), {
		title: "Talabat Retrieval Complete",
		indicator: "orange",
		message: "Retrieved 2 orders. 1 could not be imported.",
	});
});

test("Talabat retrieval expands date pickers to a full UTC-day range", () => {
	assert.deepEqual(build_utc_range({ start_date: "2026-09-01", end_date: "2026-09-03" }), {
		start: "2026-09-01T00:00:00Z",
		end: "2026-09-03T23:59:59Z",
	});
});

test("Talabat retrieval rejects invalid or reversed dates", () => {
	assert.deepEqual(build_utc_range({ start_date: "2026-02-30", end_date: "2026-09-03" }), {
		error: "Start Date and End Date must be valid dates.",
	});
	assert.deepEqual(build_utc_range({ start_date: "2026-09-04", end_date: "2026-09-03" }), {
		error: "Start Date must be before or equal to End Date.",
	});
});
