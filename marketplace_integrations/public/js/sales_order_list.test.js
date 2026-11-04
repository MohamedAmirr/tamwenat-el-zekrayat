const assert = require("node:assert/strict");
const test = require("node:test");

global.__ = (message, args) => {
	if (!args) return message;
	return message.replace(/\{(\d+)\}/g, (_, index) => args[index]);
};

const ESCAPE_MAPPING = {
	"&": "&amp;",
	"<": "&lt;",
	">": "&gt;",
	'"': "&quot;",
	"'": "&#39;",
	"`": "&#x60;",
	"=": "&#x3D;",
};

function load({ original_onload = null, original_formatters = {} } = {}) {
	const created_cards = [];
	const attached = [];
	global.frappe = {
		// Frappe's real escape_html mapping (frappe/public/js/frappe/utils/utils.js).
		utils: { escape_html: (value) => String(value).replace(/[&<>"'`=]/g, (char) => ESCAPE_MAPPING[char] || char) },
		listview_settings: {
			"Sales Order": {
				...(original_onload ? { onload: original_onload } : {}),
				formatters: original_formatters,
			},
		},
		require: () => {
			global.frappe.marketplace_integrations = {
				...global.frappe.marketplace_integrations,
				order_hover_card: {
					create: (opts) => {
						created_cards.push(opts);
						return { attach_to_list: (view) => attached.push(view) };
					},
				},
			};
			return Promise.resolve();
		},
	};
	// hooks.py loads platform_badge.js and carrier_status.js before the list script
	for (const name of ["./platform_badge.js", "./carrier_status.js"]) {
		const helper = require.resolve(name);
		delete require.cache[helper];
		require(helper);
	}
	const path = require.resolve("./sales_order_list.js");
	delete require.cache[path];
	const module_exports = require(path);
	return { created_cards, attached, module_exports, settings: global.frappe.listview_settings["Sales Order"] };
}

const settle = () => new Promise((resolve) => setTimeout(resolve, 5));

test("the list loads the platform and shipping summary without dropping ERPNext's own fields", () => {
	const { settings } = load();
	assert.ok(settings.add_fields.includes("custom_marketplace_platform"));
	assert.ok(settings.add_fields.includes("custom_shipping_status_summary"));
	global.frappe.listview_settings["Sales Order"].add_fields = ["status"];
	const path = require.resolve("./sales_order_list.js");
	delete require.cache[path];
	require(path);
	assert.deepEqual(global.frappe.listview_settings["Sales Order"].add_fields, [
		"status",
		"custom_marketplace_platform",
		"custom_shipping_status_summary",
	]);
});

test("onload attaches the order hover card and ERPNext's own onload still runs", async () => {
	let erpnext_ran = false;
	const { settings, created_cards, attached } = load({ original_onload: () => (erpnext_ran = true) });
	const view = {};
	settings.onload(view);
	await settle();
	assert.ok(erpnext_ran);
	assert.deepEqual(created_cards, [
		{ method: "marketplace_integrations.api.order_hover_card.get_sales_order_card", arg: "sales_order" },
	]);
	assert.deepEqual(attached, [view]);
});

test("a hover card that fails to load does not break the list", async () => {
	const { settings } = load();
	const original_error = console.error;
	const errors = [];
	console.error = (...args) => errors.push(args);
	global.frappe.require = () => Promise.reject(new Error("offline"));
	try {
		settings.onload({});
		await settle();
	} finally {
		console.error = original_error;
	}
	assert.equal(errors.length, 1);
});

test("loading the list script again does not wrap onload a second time", async () => {
	let erpnext_runs = 0;
	const { settings } = load({ original_onload: () => erpnext_runs++ });
	const wrapped = settings.onload;
	const path = require.resolve("./sales_order_list.js");
	delete require.cache[path];
	require(path);
	assert.equal(global.frappe.listview_settings["Sales Order"].onload, wrapped);
	settings.onload({});
	await settle();
	assert.equal(erpnext_runs, 1);
});

test("the shipping summary renders translated, escaped pills with counts and keeps other formatters", () => {
	const existing = () => "existing";
	const { settings } = load({ original_formatters: { another_field: existing } });
	assert.equal(settings.formatters.another_field, existing);
	const format = settings.formatters.custom_shipping_status_summary;
	assert.equal(
		format('{"Delivered": 2}'),
		'<span class="indicator-pill green">Delivered <bdi dir="ltr">2</bdi></span>'
	);
	assert.equal(
		format({ "<unsafe>": 1 }),
		'<span class="indicator-pill grey">&lt;unsafe&gt; <bdi dir="ltr">1</bdi></span>'
	);
});

test("the shipping summary ignores empty, malformed and non-count values", () => {
	const { settings } = load();
	const format = settings.formatters.custom_shipping_status_summary;
	for (const value of ["", null, undefined, "not json", "[1,2]", "5", '{"Delivered": 0}', '{"Delivered": "2"}', '{"Delivered": -1}']) {
		assert.equal(format(value), "", String(value));
	}
});

// -- platform column and carrier-status placeholder -----------------------------------------

test("the platform column renders a colored filterable pill for each platform", () => {
	const { settings } = load();
	const format = settings.formatters.custom_marketplace_platform;
	const df = { fieldname: "custom_marketplace_platform", label: "Marketplace Platform" };
	assert.equal(
		format("Noon", df),
		'<a class="filterable ellipsis" data-filter="custom_marketplace_platform,&#x3D;,Noon" title="Marketplace Platform: Noon"><span class="indicator-pill yellow">Noon</span></a>'
	);
	assert.match(format("Shopify", df), /indicator-pill green">Shopify/);
	assert.match(format("Talabat", df), /indicator-pill orange">Talabat/);
	assert.match(format("Bosta", df), /indicator-pill grey">Bosta/);
	assert.equal(format("", df), "");
	assert.ok(!format("<b>", df).includes("<b>"));
});

test("a submitted marketplace order with no carrier status shows one grey placeholder pill", () => {
	const { settings } = load();
	const format = settings.formatters.custom_shipping_status_summary;
	const expected = '<span class="indicator-pill grey">No shipment yet</span>';
	for (const value of ["", null, undefined, "not json", '{"Delivered": 0}']) {
		assert.equal(format(value, {}, marketplace_order(value)), expected, String(value));
	}
	for (const status of ["To Deliver", "To Deliver and Bill"]) {
		assert.equal(format("", {}, marketplace_order("", { status })), expected, status);
	}
});

test("the placeholder is only for To Deliver and To Deliver and Bill, not Closed, Completed, On Hold, To Bill", () => {
	const { settings } = load();
	const format = settings.formatters.custom_shipping_status_summary;
	for (const status of ["Closed", "Completed", "On Hold", "To Bill", "Draft", "Cancelled", undefined]) {
		assert.equal(format("", {}, marketplace_order("", { status })), "", String(status));
	}
});

test("the placeholder is not shown for drafts, cancelled, non-marketplace orders or without a doc", () => {
	const { settings } = load();
	const format = settings.formatters.custom_shipping_status_summary;
	assert.equal(format("", {}, marketplace_order("", { docstatus: 0 })), "");
	assert.equal(format("", {}, marketplace_order("", { docstatus: 2, status: "Cancelled" })), "");
	assert.equal(format("", {}, marketplace_order("", { custom_marketplace_platform: null })), "");
	assert.equal(format("", {}, marketplace_order("", { custom_marketplace_platform: "" })), "");
	assert.equal(format(""), "");
});

test("a real summary wins over the placeholder", () => {
	const { settings } = load();
	const format = settings.formatters.custom_shipping_status_summary;
	assert.equal(
		format('{"Delivered": 2}', {}, marketplace_order('{"Delivered": 2}')),
		'<span class="indicator-pill green">Delivered <bdi dir="ltr">2</bdi></span>'
	);
});

// -- indicator ---------------------------------------------------------------------------

const marketplace_order = (summary, extra = {}) => ({
	docstatus: 1,
	status: "To Deliver and Bill",
	custom_marketplace_platform: "Noon",
	custom_shipping_status_summary: summary,
	...extra,
});

test("a marketplace order with a summary shows the carrier status instead of the native indicator", () => {
	const { settings } = load();
	assert.deepEqual(
		settings.get_indicator(marketplace_order('{"Delivered": 2}')).slice(0, 2),
		["Delivered", "green"]
	);
});

test("native indicator stays for non-marketplace orders and empty summaries", () => {
	const native = () => ["To Deliver", "orange", "native"];
	load();
	// ERPNext's own get_indicator is present before our file loads
	global.frappe.listview_settings["Sales Order"] = { get_indicator: native, formatters: {} };
	const path = require.resolve("./sales_order_list.js");
	delete require.cache[path];
	require(path);
	const wrapped = global.frappe.listview_settings["Sales Order"].get_indicator;
	assert.notEqual(wrapped, native);
	const kept = ["To Deliver", "orange", "native"];
	assert.deepEqual(wrapped({ docstatus: 1, status: "To Deliver", custom_shipping_status_summary: '{"Delivered": 1}' }), kept);
	assert.deepEqual(wrapped(marketplace_order("")), kept);
	assert.deepEqual(wrapped(marketplace_order(null)), kept);
	assert.deepEqual(wrapped(marketplace_order("not json")), kept);
	assert.deepEqual(wrapped(marketplace_order('{"Delivered": 1}')).slice(0, 2), ["Delivered", "green"]);
});

test("ERPNext's Closed, On Hold, draft and cancelled indicators are kept for marketplace orders", () => {
	const { module_exports } = load();
	for (const extra of [{ status: "Closed" }, { status: "On Hold" }, { docstatus: 0 }, { docstatus: 2, status: "Cancelled" }, { advance_payment_status: "Requested" }]) {
		assert.equal(module_exports.external_indicator(marketplace_order('{"Delivered": 1}', extra)), null, JSON.stringify(extra));
	}
});

test("Partially Delivered, mixed and problem states in the indicator and the column", () => {
	const { module_exports, settings } = load();
	const indicator = (summary) => module_exports.external_indicator(marketplace_order(summary)).slice(0, 2);
	assert.deepEqual(indicator('{"Delivered": 1, "Shipped": 2}'), ["Partially Delivered", "orange"]);
	assert.deepEqual(indicator('{"Delivered": 1, "Exception": 1}'), ["Partially Delivered, Exception 1", "red"]);
	assert.deepEqual(indicator('{"Shipped": 1, "Exception": 1}'), ["Shipped 1, Exception 1", "red"]);
	assert.deepEqual(indicator('{"Shipped": 1, "Acknowledged": 1}'), ["Shipped 1, Acknowledged 1", "orange"]);
	assert.deepEqual(indicator('{"Shipped": 3}'), ["Shipped", "blue"]);
	assert.equal(module_exports.external_indicator(marketplace_order("")), null);
	assert.equal(module_exports.external_indicator({ ...marketplace_order('{"Delivered": 1}'), custom_marketplace_platform: null }), null);

	const format = settings.formatters.custom_shipping_status_summary;
	assert.equal(
		format('{"Delivered": 2, "Shipped": 1, "Lost": 1}'),
		'<span class="indicator-pill orange">Partially Delivered</span> <span class="indicator-pill red">Lost <bdi dir="ltr">1</bdi></span>'
	);
	assert.equal(
		format('{"Shipped": 1, "Exception": 1}'),
		'<span class="indicator-pill blue">Shipped <bdi dir="ltr">1</bdi></span> <span class="indicator-pill red">Exception <bdi dir="ltr">1</bdi></span>'
	);
});

test("the indicator wrap is applied once", () => {
	load();
	const first = global.frappe.listview_settings["Sales Order"].get_indicator;
	const path = require.resolve("./sales_order_list.js");
	delete require.cache[path];
	require(path);
	assert.equal(global.frappe.listview_settings["Sales Order"].get_indicator, first);
});
