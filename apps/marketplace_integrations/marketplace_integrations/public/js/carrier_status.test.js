const assert = require("node:assert/strict");
const test = require("node:test");

global.__ = (message) => message;
global.frappe = { utils: { escape_html: (value) => String(value).replace(/</g, "&lt;") } };
const shared = require("./carrier_status.js");

const EXPECTED = {
	green: ["Delivered", "Fulfilled"],
	blue: [
		"Pickup requested",
		"Route Assigned",
		"Picking up",
		"Picked up",
		"Picked up from business",
		"Picking up from consignee",
		"Picked up from consignee",
		"Received at warehouse",
		"In transit between Hubs",
		"Acknowledged",
		"Shipped",
		"Created",
	],
	orange: ["Waiting for route", "On hold", "Returned to business", "Returned to stock", "Mixed", "Partially Delivered"],
	red: ["Exception", "Lost", "Damaged", "Investigation", "Awaiting your action", "Terminated", "Out of Stock"],
	grey: ["Canceled", "Cancelled", "Archived"],
};

test("every confirmed state has its confirmed color", () => {
	for (const [color, states] of Object.entries(EXPECTED)) {
		for (const state of states) assert.equal(shared.color_of(state), color, state);
	}
});

test("an unknown state is grey and the helper registers itself on frappe", () => {
	assert.equal(shared.color_of("Something new"), "grey");
	assert.equal(global.frappe.marketplace_integrations.carrier_status, shared);
});

test("every state of the Delivery Note Shipping Status field has a color entry", () => {
	const fs = require("node:fs");
	const path = require("node:path");
	const fixture = JSON.parse(fs.readFileSync(path.join(__dirname, "../../fixtures/custom_field.json"), "utf8"));
	const field = fixture.find((row) => row.name === "Delivery Note-custom_shipping_status");
	const states = field.options.split("\n").filter(Boolean);
	for (const state of states) assert.ok(shared.STATUS_COLORS[state], `no color for ${state}`);
	assert.equal(field.in_list_view, 1);
});

test("pill is escaped and status_pill is empty without a status", () => {
	assert.equal(shared.pill("<x>"), '<span class="indicator-pill grey">&lt;x></span>');
	assert.equal(shared.status_pill(""), "");
	assert.equal(shared.status_pill(null), "");
	assert.equal(shared.status_pill("Shipped"), '<span class="indicator-pill blue">Shipped</span>');
});

test("classify follows the notes' statuses, not quantities", () => {
	assert.equal(shared.classify([["Delivered", 5]]).kind, "single");
	assert.equal(shared.classify([["Delivered", 1], ["Shipped", 9]]).kind, "partial");
	assert.equal(shared.classify([["Shipped", 1], ["Exception", 1]]).kind, "mixed");
	assert.deepEqual(shared.classify([["Delivered", 1], ["Lost", 1]]).problems, [["Lost", 1]]);
});

test("a problem state is red next to Partially Delivered; mixed keeps the counts pills", () => {
	for (const problem of ["Exception", "Lost", "Damaged", "Investigation", "Terminated"]) {
		const html = shared.summary_html([["Delivered", 1], [problem, 1]]);
		assert.match(html, /indicator-pill orange">Partially Delivered</);
		assert.match(html, new RegExp(`indicator-pill red">${problem} `));
	}
	assert.equal(shared.summary_html([]), "");
});

test("Awaiting your action and Out of Stock show red beside Partially Delivered", () => {
	for (const problem of ["Awaiting your action", "Out of Stock"]) {
		const html = shared.summary_html([["Delivered", 1], [problem, 1]]);
		assert.match(html, /indicator-pill orange">Partially Delivered</, problem);
		assert.match(html, new RegExp(`indicator-pill red">${problem} `), problem);
	}
});
