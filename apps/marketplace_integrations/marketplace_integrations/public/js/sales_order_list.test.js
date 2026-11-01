const assert = require("node:assert/strict");
const test = require("node:test");

global.__ = (message, args) => {
	if (!args) return message;
	return message.replace(/\{(\d+)\}/g, (_, index) => args[index]);
};

function load({ original_onload = null, original_formatters = {} } = {}) {
	const created_cards = [];
	const attached = [];
	global.frappe = {
		utils: { escape_html: (value) => String(value).replace(/</g, "&lt;") },
		listview_settings: {
			"Sales Order": {
				...(original_onload ? { onload: original_onload } : {}),
				formatters: original_formatters,
			},
		},
		require: () => {
			global.frappe.marketplace_integrations = {
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
		format('{"Delivered": 2, "Exception": 1}'),
		'<span class="indicator-pill green">Delivered <bdi dir="ltr">2</bdi></span> <span class="indicator-pill red">Exception <bdi dir="ltr">1</bdi></span>'
	);
	assert.equal(
		format({ "<unsafe>": 1 }),
		'<span class="indicator-pill grey">&lt;unsafe> <bdi dir="ltr">1</bdi></span>'
	);
});

test("the shipping summary ignores empty, malformed and non-count values", () => {
	const { settings } = load();
	const format = settings.formatters.custom_shipping_status_summary;
	for (const value of ["", null, undefined, "not json", "[1,2]", "5", '{"Delivered": 0}', '{"Delivered": "2"}', '{"Delivered": -1}']) {
		assert.equal(format(value), "", String(value));
	}
});
