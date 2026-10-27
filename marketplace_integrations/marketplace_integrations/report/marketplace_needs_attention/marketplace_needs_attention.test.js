const assert = require("node:assert/strict");
const test = require("node:test");

global.__ = (message) => message;
global.frappe = {
	query_reports: {},
	utils: {
		escape_html(value) {
			return String(value == null ? "" : value)
				.replace(/&/g, "&amp;")
				.replace(/</g, "&lt;")
				.replace(/>/g, "&gt;")
				.replace(/"/g, "&quot;");
		},
	},
};

const { MARKETPLACE_ATTENTION_PROBLEM_TYPES, linked_document_html, next_action_html, reference_html } = require("./marketplace_needs_attention.js");

const REPORT = () => frappe.query_reports["Marketplace Needs Attention"];

test("the report is registered with platform, account and problem filters", () => {
	assert.deepEqual(
		REPORT().filters.map((filter) => filter.fieldname),
		["platform", "account", "problem_type"]
	);
	assert.deepEqual(REPORT().filters[2].options.slice(1), MARKETPLACE_ATTENTION_PROBLEM_TYPES);
});

test("Open Order and View Error link to the referenced document", () => {
	assert.equal(
		next_action_html({ next_action: "Open Order", reference_doctype: "Marketplace Order", reference_name: "MKTORD-00001" }),
		'<a href="/app/marketplace-order/MKTORD-00001">Open Order</a>'
	);
	assert.equal(
		next_action_html({ next_action: "View Error", reference_doctype: "Marketplace Sync Log", reference_name: "MSL-00001" }),
		'<a href="/app/marketplace-sync-log/MSL-00001">View Error</a>'
	);
});

test("Map Item opens a new mapping prefilled with the account and reference, URL-encoded", () => {
	const html = next_action_html({
		next_action: "Map Item",
		account: "MKTACC-1",
		next_action_reference: 'A B&C"',
		reference_doctype: "Marketplace Order",
		reference_name: "MKTORD-1",
	});
	assert.match(html, /href="\/app\/marketplace-product-mapping\/new\?marketplace_account=MKTACC-1&amp;external_reference=A%20B%26C%22"/);
});

test("Retry carries the escaped order name as a data attribute instead of a navigation link", () => {
	const html = next_action_html({ next_action: "Retry", reference_name: 'MKTORD-"1' });
	assert.match(html, /data-attention-retry="MKTORD-&quot;1"/);
	assert.match(html, /href="#"/);
});

test("the explanation column is escaped", () => {
	const html = REPORT().formatter("x", {}, { fieldname: "explanation" }, { explanation: "<img src=x onerror=1>" }, (v) => v);
	assert.doesNotMatch(html, /<img/);
	assert.match(html, /&lt;img/);
});

test("Map Item opens the existing disabled mapping instead of a /new form", () => {
	const html = next_action_html({
		next_action: "Map Item",
		account: "MKTACC-1",
		next_action_reference: "SKU-OFF",
		next_action_mapping: "MPM 1/2",
		reference_doctype: "Marketplace Order",
		reference_name: "MKTORD-1",
	});
	assert.match(html, /href="\/app\/marketplace-product-mapping\/MPM%201%2F2"/);
	assert.doesNotMatch(html, /\/new/);
});

test("onload binds a namespaced click handler after clearing the previous one, so reloads do not stack", () => {
	const calls = [];
	const wrapper = {
		off(event) {
			calls.push(["off", event]);
			return wrapper;
		},
		on(event, selector, handler) {
			calls.push(["on", event, selector, typeof handler]);
			return wrapper;
		},
	};
	const report = { page: { wrapper } };
	REPORT().onload(report);
	REPORT().onload(report);
	assert.deepEqual(calls, [
		["off", "click.attention"],
		["on", "click.attention", "[data-attention-retry]", "function"],
		["off", "click.attention"],
		["on", "click.attention", "[data-attention-retry]", "function"],
	]);
});

test("the reference shows the label with the docname in the tooltip, LTR-isolated and escaped", () => {
	const html = reference_html({
		reference_doctype: "Marketplace Order",
		reference_name: "MKTORD-00001",
		reference_label: "#1027<b>",
	});
	assert.match(html, /<bdi dir="ltr">#1027&lt;b&gt;<\/bdi>/);
	assert.match(html, /title="MKTORD-00001"/);
	assert.match(html, /href="\/app\/marketplace-order\/MKTORD-00001"/);
});

test("a reference without a label falls back to the docname", () => {
	assert.match(
		reference_html({ reference_doctype: "Bosta Shipment", reference_name: "BOSTA-SHP-00001" }),
		/<bdi dir="ltr">BOSTA-SHP-00001<\/bdi>/
	);
});

test("a Needs Action row links its blocking or prepared document and escapes the name", () => {
	assert.ok(MARKETPLACE_ATTENTION_PROBLEM_TYPES.includes("Needs Action"));
	assert.ok(!MARKETPLACE_ATTENTION_PROBLEM_TYPES.includes("Modified After Sync"));
	const html = linked_document_html({ linked_doctype: "Delivery Note", linked_document: "MAT-DN-1" });
	assert.match(html, /href="\/app\/delivery-note\/MAT-DN-1"/);
	assert.equal(linked_document_html({ linked_doctype: "Delivery Note" }), "");
	assert.doesNotMatch(
		linked_document_html({ linked_doctype: "Delivery Note", linked_document: "<img onerror=x>" }),
		/<img/
	);
});
