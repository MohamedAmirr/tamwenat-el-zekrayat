const assert = require("node:assert/strict");
const test = require("node:test");

global.frappe = {
	query_reports: {},
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
	MARKETPLACE_CHANNEL_STATUS_LEVEL_COLORS,
	orders_needing_attention_url,
	product_sync_errors_url,
	import_errors_url,
	to_query_string,
} = require("./marketplace_channel_status.js");

test("every defined level has a color and no stray levels are introduced", () => {
	assert.deepEqual(Object.keys(MARKETPLACE_CHANNEL_STATUS_LEVEL_COLORS).sort(), [
		"Disabled",
		"Error",
		"OK",
		"Warning",
	]);
	assert.equal(MARKETPLACE_CHANNEL_STATUS_LEVEL_COLORS.OK, "green");
	assert.equal(MARKETPLACE_CHANNEL_STATUS_LEVEL_COLORS.Warning, "orange");
	assert.equal(MARKETPLACE_CHANNEL_STATUS_LEVEL_COLORS.Error, "red");
	assert.equal(MARKETPLACE_CHANNEL_STATUS_LEVEL_COLORS.Disabled, "grey");
});

test("the formatter is registered under the report's exact name", () => {
	assert.ok(frappe.query_reports["Marketplace Channel Status"]);
	assert.equal(typeof frappe.query_reports["Marketplace Channel Status"].formatter, "function");
});

test("orders_needing_attention_url opens the Needs Attention report for the account", () => {
	assert.equal(
		orders_needing_attention_url("MKTACC-1"),
		"/app/query-report/Marketplace%20Needs%20Attention?account=MKTACC-1"
	);
});

test("product_sync_errors_url scopes to the account; import_errors_url opens the Needs Attention report", () => {
	assert.match(product_sync_errors_url("MKTACC-2"), /^\/app\/marketplace-product-mapping\?marketplace_account=MKTACC-2$/);
	assert.equal(import_errors_url("MKTACC-2"), "/app/query-report/Marketplace%20Needs%20Attention?account=MKTACC-2");
});

test("to_query_string encodes special characters in account names", () => {
	const query = to_query_string({ marketplace_account: "MKTACC & <1>" });
	assert.equal(query, "marketplace_account=MKTACC%20%26%20%3C1%3E");
});

test("formatter colors the Channel Status cell by its known level", () => {
	const formatter = frappe.query_reports["Marketplace Channel Status"].formatter;
	const default_formatter = (value) => value;
	const html = formatter("Error", {}, { fieldname: "level" }, { level: "Error" }, default_formatter);
	assert.match(html, /color: red/);
	assert.match(html, />Error<\/span>/);
});

test("formatter escapes an unrecognized/malicious level value instead of executing it", () => {
	const formatter = frappe.query_reports["Marketplace Channel Status"].formatter;
	const default_formatter = (value) => value;
	const html = formatter("x", {}, { fieldname: "level" }, { level: "<script>evil()</script>" }, default_formatter);
	assert.doesNotMatch(html, /<script>/);
	assert.match(html, /&lt;script&gt;/);
});

test("formatter links a non-zero count cell to the filtered list and leaves a zero count plain", () => {
	const formatter = frappe.query_reports["Marketplace Channel Status"].formatter;
	const default_formatter = (value) => value;

	const linked = formatter(
		"3",
		{},
		{ fieldname: "orders_needing_attention" },
		{ account: "MKTACC-3", orders_needing_attention: 3 },
		default_formatter
	);
	assert.match(linked, /^<a href="\/app\/query-report\/Marketplace%20Needs%20Attention\?/);

	const plain = formatter(
		"0",
		{},
		{ fieldname: "orders_needing_attention" },
		{ account: "MKTACC-3", orders_needing_attention: 0 },
		default_formatter
	);
	assert.equal(plain, "0");
});

test("formatter links a non-zero Fulfillment Errors count to the Needs Attention report", () => {
	const formatter = frappe.query_reports["Marketplace Channel Status"].formatter;
	const linked = formatter(
		"2",
		{},
		{ fieldname: "fulfillment_errors" },
		{ account: "MKTACC-4", fulfillment_errors: 2 },
		(value) => value
	);
	assert.match(linked, /^<a href="\/app\/query-report\/Marketplace%20Needs%20Attention\?account=MKTACC-4"/);
});
