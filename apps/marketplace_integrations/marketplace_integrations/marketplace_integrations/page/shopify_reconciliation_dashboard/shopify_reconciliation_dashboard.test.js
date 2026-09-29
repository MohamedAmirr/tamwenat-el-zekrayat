const assert = require("node:assert/strict");
const test = require("node:test");

global.__ = (message, values = []) =>
	values.reduce((text, value, index) => text.replace(`{${index}}`, String(value)), message);

global.frappe = {
	pages: { "shopify-reconciliation-dashboard": {} },
	datetime: {
		comment_when: (datetime_str) =>
			`<span class="frappe-timestamp" data-timestamp="${datetime_str}">a moment ago</span>`,
	},
	utils: { escape_html: (text) => text },
};

const { build_last_sweep_message } = require("./shopify_reconciliation_dashboard.js");

test("no sweep yet renders as plain text", () => {
	const message = build_last_sweep_message(null);
	assert.equal(message.mode, "text");
	assert.equal(message.content, "No sweep has run yet.");
});

test("a failed sweep renders as plain text, keeping a hostile error string escaped", () => {
	const message = build_last_sweep_message({ status: "failed", error: "<img onerror=alert(1)>" });
	assert.equal(message.mode, "text");
	assert.equal(message.content, "Last sweep failed: <img onerror=alert(1)>");
});

test("a completed sweep renders as HTML with a real frappe-timestamp span, not escaped text", () => {
	const message = build_last_sweep_message({
		status: "completed",
		completed_at: "2026-09-29 14:09:10.307390",
		result: {
			total_exceptions: 2,
			exceptions: [{ category: "Unmapped" }, { category: "Deleted" }],
		},
	});

	assert.equal(message.mode, "html");
	// The timestamp must appear as a real <span> element in the HTML that
	// will be inserted via .html(), never as an escaped "&lt;span...&gt;"
	// string - that escaping was the original bug.
	assert.match(message.content, /<span class="frappe-timestamp"/);
	assert.doesNotMatch(message.content, /&lt;span/);
	assert.match(message.content, /2 exception\(s\)/);
});

test("no completed_at falls back to 'unknown' without calling comment_when", () => {
	const message = build_last_sweep_message({
		status: "completed",
		result: { total_exceptions: 0, exceptions: [] },
	});

	assert.equal(message.mode, "html");
	assert.match(message.content, /Last swept unknown\./);
});
