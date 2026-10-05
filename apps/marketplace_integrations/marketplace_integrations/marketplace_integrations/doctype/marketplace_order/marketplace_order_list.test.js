const assert = require("node:assert/strict");
const test = require("node:test");

global.__ = (message, values = []) =>
	values.reduce(
		(formatted, value, index) => formatted.replace(`{${index}}`, String(value)),
		message
	);

global.frappe = {
	listview_settings: {},
	utils: {
		escape_html(value) {
			return String(value == null ? "" : value)
				.replace(/&/g, "&amp;")
				.replace(/</g, "&lt;")
				.replace(/>/g, "&gt;");
		},
	},
};

const {
	build_utc_range,
	format_import_result,
	format_noon_import_result,
	format_retry_summary,
	get_order_indicator,
	platform_status_text,
	retry_erpnext_sync,
	settle_with_concurrency,
	show_refunded_orders,
	summarize_retry_results,
} = require("./marketplace_order_list.js");

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

test("Show Refunded Orders sets a filter to Shopify orders with a refund financial status", () => {
	const filters_added = [];
	const listview = { filter_area: { add: (filters) => filters_added.push(filters) } };

	show_refunded_orders(listview);

	assert.equal(filters_added.length, 1);
	assert.deepEqual(filters_added[0], [
		["Marketplace Order", "platform", "=", "Shopify"],
		["Marketplace Order", "external_financial_status", "in", ["REFUNDED", "PARTIALLY_REFUNDED"]],
	]);
});

test("indicator colours each ERPNext sync status and falls back to Not Synced/grey", () => {
	assert.deepEqual(get_order_indicator({ erpnext_sync_status: "Synced" }), [
		"Synced",
		"green",
		"erpnext_sync_status,=,Synced",
	]);
	assert.equal(get_order_indicator({ erpnext_sync_status: "Not Synced" })[1], "grey");
	assert.equal(get_order_indicator({ erpnext_sync_status: "Modified After Sync" })[1], "orange");
	assert.equal(get_order_indicator({ erpnext_sync_status: "Sync Error" })[1], "red");
	assert.deepEqual(get_order_indicator({}), ["Not Synced", "grey", "erpnext_sync_status,=,Not Synced"]);
});

test("platform status shows Shopify financial/fulfillment and external_status elsewhere", () => {
	assert.equal(
		platform_status_text({
			platform: "Shopify",
			external_financial_status: "PAID",
			external_fulfillment_status: "FULFILLED",
		}),
		"PAID / FULFILLED"
	);
	assert.equal(platform_status_text({ platform: "Shopify", external_financial_status: "PAID" }), "PAID");
	assert.equal(platform_status_text({ platform: "Talabat", external_status: "RECEIVED" }), "RECEIVED");
	assert.equal(platform_status_text({ platform: "Shopify", external_status: "OPEN" }), "OPEN");
});

test("the external_status column formatter escapes platform-sourced text", () => {
	const formatter = frappe.listview_settings["Marketplace Order"].formatters.external_status;
	const html = formatter("x", {}, { platform: "Talabat", external_status: "<img src=x onerror=1>" });
	assert.doesNotMatch(html, /<img/);
	assert.match(html, /&lt;img/);
});

test("retry summary separates succeeded from failed, including {ok: false} blockers and server messages", () => {
	const rejection = { responseJSON: { _server_messages: JSON.stringify([JSON.stringify({ message: "<b>Not</b> permitted &amp; blocked" })]) } };
	const summary = summarize_retry_results(
		["MKTORD-1", "MKTORD-2", "MKTORD-3", "MKTORD-4"],
		[
			{ status: "fulfilled", value: { message: { ok: true } } },
			{ status: "fulfilled", value: { message: { ok: false, error: "No mapping" } } },
			{ status: "rejected", reason: rejection },
			{ status: "rejected", reason: {} },
		]
	);
	assert.deepEqual(summary.succeeded, ["MKTORD-1"]);
	assert.deepEqual(summary.failed, [
		{ name: "MKTORD-2", text: "No mapping" },
		{ name: "MKTORD-3", text: "Not permitted & blocked" },
		{ name: "MKTORD-4", text: "The request failed." },
	]);
});

test("retry summary escapes failure text and reflects the outcome in the indicator", () => {
	const html = format_retry_summary({
		succeeded: ["MKTORD-1"],
		failed: [{ name: "MKTORD-2", text: "<script>x</script>" }],
	});
	assert.equal(html.indicator, "orange");
	assert.doesNotMatch(html.message, /<script>/);
	assert.match(html.message, /Succeeded: 1/);
	assert.match(html.message, /Failed: 1/);
	assert.equal(format_retry_summary({ succeeded: ["a"], failed: [] }).indicator, "green");
});

test("Noon retrieval result distinguishes queued from already queued", () => {
	assert.equal(format_noon_import_result({ queued: true }).indicator, "green");
	assert.equal(format_noon_import_result({ queued: false }).indicator, "orange");
	assert.equal(format_noon_import_result(undefined).indicator, "orange");
});

test("settle_with_concurrency never exceeds the limit, keeps input order and captures rejections", async () => {
	let in_flight = 0;
	let max_in_flight = 0;
	const results = await settle_with_concurrency([1, 2, 3, 4, 5, 6, 7], 3, async (value) => {
		in_flight++;
		max_in_flight = Math.max(max_in_flight, in_flight);
		await new Promise((resolve) => setTimeout(resolve, 5));
		in_flight--;
		if (value === 4) throw new Error("boom");
		return value * 10;
	});
	assert.equal(max_in_flight, 3);
	assert.deepEqual(results.map((result) => result.status), [
		"fulfilled", "fulfilled", "fulfilled", "rejected", "fulfilled", "fulfilled", "fulfilled",
	]);
	assert.equal(results[0].value, 10);
	assert.equal(results[3].reason.message, "boom");
	assert.deepEqual(await settle_with_concurrency([], 3, async () => 1), []);
});

test("retry action runs sync calls with a small concurrency limit and one summary", async () => {
	let in_flight = 0;
	let max_in_flight = 0;
	let summary;
	global.frappe.call = async () => {
		in_flight++;
		max_in_flight = Math.max(max_in_flight, in_flight);
		await new Promise((resolve) => setTimeout(resolve, 5));
		in_flight--;
		return { message: { ok: true } };
	};
	global.frappe.msgprint = (value) => {
		summary = value;
	};
	const names = ["A", "B", "C", "D", "E"];
	const listview = {
		get_checked_items: () => names,
		clear_checked_items() {},
		refresh() {},
	};
	await retry_erpnext_sync(listview);
	assert.ok(max_in_flight <= 3);
	assert.match(summary.message, /Succeeded: 5/);
});
