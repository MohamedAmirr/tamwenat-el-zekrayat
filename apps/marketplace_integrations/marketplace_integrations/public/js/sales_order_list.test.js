const assert = require("node:assert/strict");
const test = require("node:test");

global.__ = (message, args) => {
	if (!args) return message;
	return message.replace(/\{(\d+)\}/g, (_, index) => args[index]);
};

function load({ excluded = ["Noon", "Talabat"], creator = true, original_onload = null, original_formatters = {}, defaults = null, defaults_fail = false } = {}) {
	const msgprints = [];
	const alerts = [];
	const calls = [];
	const dialogs = [];
	const created_cards = [];
	const attached = [];
	global.frappe = {
		user: { has_role: () => creator },
		utils: { escape_html: (value) => String(value).replace(/</g, "&lt;") },
		listview_settings: {
			"Sales Order": {
				...(original_onload ? { onload: original_onload } : {}),
				formatters: original_formatters,
			},
		},
		msgprint: (message) => msgprints.push(message),
		show_alert: (alert) => alerts.push(alert),
		// Loading sales_order.js is what exports the creator roles.
		require: () => {
			global.frappe.marketplace_integrations = {
				BOSTA_CREATOR_ROLES: ["Sales Manager"],
				BOSTA_EXCLUDED_PLATFORMS: excluded,
				order_hover_card: {
					create: (opts) => {
						created_cards.push(opts);
						return { attach_to_list: (view) => attached.push(view) };
					},
				},
			};
			return Promise.resolve();
		},
		call: (opts) => {
			calls.push(opts);
			if (opts.method.endsWith("get_create_defaults")) {
				return defaults_fail ? Promise.reject(new Error("boom")) : Promise.resolve({ message: defaults });
			}
			return Promise.resolve({ message: { skipped: [], ready: [], review: [] } });
		},
		ui: {
			Dialog: class {
				constructor(config) {
					this.config = config;
					dialogs.push(this);
				}
				set_primary_action(label, action) {
					this.primary = { label, action };
				}
				show() {}
				hide() {}
			},
		},
	};
	const path = require.resolve("./sales_order_list.js");
	delete require.cache[path];
	const module_exports = require(path);
	return { calls, alerts, msgprints, dialogs, created_cards, attached, module_exports, settings: global.frappe.listview_settings["Sales Order"] };
}

function listview(names = []) {
	const items = [];
	return {
		items,
		page: { add_actions_menu_item: (label, action) => items.push({ label, action }) },
		get_checked_items: () => names,
		refresh() {},
	};
}

test("creators get the bulk action and ERPNext's own onload still runs", async () => {
	let erpnext_ran = false;
	const { settings } = load({ original_onload: () => (erpnext_ran = true) });
	const view = listview();
	settings.onload(view);
	await new Promise((resolve) => setTimeout(resolve, 5));
	assert.ok(erpnext_ran);
	assert.deepEqual(view.items.map((item) => item.label), ["Create Bosta Shipments"]);
});

test("users without a creator role do not get the action", async () => {
	const { settings } = load({ creator: false });
	const view = listview();
	settings.onload(view);
	await new Promise((resolve) => setTimeout(resolve, 5));
	assert.deepEqual(view.items, []);
});

// ---- the Bosta bulk action leaves out platforms that fulfil their own orders (#368) ----

const rows = (...pairs) => pairs.map(([name, platform]) => ({ name, custom_marketplace_platform: platform }));
const settle = () => new Promise((resolve) => setTimeout(resolve, 5));
const plan_call = (calls) => calls.find((call) => call.method.endsWith("get_bulk_create_plan"));

test("the list loads each row's platform without dropping ERPNext's own fields", () => {
	const { settings } = load();
	assert.ok(settings.add_fields.includes("custom_marketplace_platform"));
	global.frappe.listview_settings["Sales Order"].add_fields = ["status"];
	const path = require.resolve("./sales_order_list.js");
	delete require.cache[path];
	require(path);
	assert.deepEqual(global.frappe.listview_settings["Sales Order"].add_fields, ["status", "custom_marketplace_platform"]);
});

test("none excluded: Shopify and rows without a platform go to the server exactly as before", async () => {
	const { module_exports, calls, alerts, msgprints } = load();
	module_exports.start(listview(rows(["SO-1", "Shopify"], ["SO-2", ""], ["SO-3", undefined])));
	await settle();
	assert.deepEqual(plan_call(calls).args, { sales_orders: ["SO-1", "SO-2", "SO-3"] });
	assert.deepEqual(alerts, []);
	assert.deepEqual(msgprints, []);
});

test("all excluded: nothing is requested and the user is told Bosta is not used for these platforms", async () => {
	const { module_exports, calls, msgprints } = load();
	module_exports.start(listview(rows(["SO-1", "Noon"], ["SO-2", "Talabat"])));
	await settle();
	assert.equal(plan_call(calls), undefined);
	assert.match(msgprints[0].message, /Bosta is not used for Noon, Talabat orders/);
});

test("mixed selection: only the non-excluded rows are sent and the alert counts the rows left out", async () => {
	const { module_exports, calls, alerts } = load();
	module_exports.start(listview(rows(["SO-1", "Noon"], ["SO-2", "Shopify"], ["SO-3", "Talabat"], ["SO-4", ""])));
	await settle();
	assert.deepEqual(plan_call(calls).args, { sales_orders: ["SO-2", "SO-4"] });
	assert.equal(alerts.length, 1);
	assert.match(alerts[0].message, /^2 selected Sales Order\(s\) were left out: Bosta is not used for Noon, Talabat orders\.$/);
});

test("split_by_platform treats a missing or unknown platform as allowed and an empty list as nothing excluded", () => {
	const { module_exports } = load();
	const selection = module_exports.selected_rows(listview(rows(["A", "Noon"], ["B", undefined], ["C", "Amazon"])));
	assert.deepEqual(module_exports.split_by_platform(selection, ["Noon", "Talabat"]), { allowed: ["B", "C"], left_out: 1 });
	assert.deepEqual(module_exports.split_by_platform(selection, []), { allowed: ["A", "B", "C"], left_out: 0 });
	assert.deepEqual(module_exports.split_by_platform(selection, undefined), { allowed: ["A", "B", "C"], left_out: 0 });
});

test("more than 50 selected orders are refused naming 50 and nothing is requested", () => {
	const { module_exports, calls, msgprints } = load();
	module_exports.start(listview(Array.from({ length: 51 }, (_, index) => `SO-${index}`)));
	assert.equal(calls.length, 0);
	assert.match(msgprints[0], /at most 50/);
});

test("the plan summary lists each skipped order with its reason before anything is sent", () => {
	const { module_exports, dialogs, calls } = load();
	module_exports.confirm_plan(listview(), {
		ready: [{ sales_order: "SO-1" }],
		review: [],
		skipped: [{ sales_order: "SO-2", reason: "Sales Order SO-2 is Closed." }],
	});
	assert.match(dialogs[0].config.fields[0].options, /SO-2: Sales Order SO-2 is Closed\./);
	assert.equal(dialogs[0].primary.label, "Continue");
	assert.equal(calls.length, 0);
});

const USED = {
	sales_order: "SO-1",
	receiver_name: "Ahmed",
	receiver_phone: "0100",
	city_id: "c-cairo",
	address_first_line: "12 Example Street",
	expected_cod: 150,
};

test("the bulk send maps a ready order to its city ID and sends no district", async () => {
	const { module_exports, calls } = load();
	// The queued job opens a status panel; only its constructor matters here.
	global.frappe.ui.ShopifyCatalogPushStatus = class {};
	global.frappe.ui.ShopifyCatalogPushStatus.BOSTA_BULK_OPTIONS = {};
	await module_exports.review_and_send(listview(), {
		ready: [
			{
				sales_order: "SO-1",
				defaults: {
					receiver_name: "Ahmed",
					receiver_phone: "0100",
					city_id: "c-cairo",
					address_first_line: "12 Example Street",
					cod_amount: 150,
				},
			},
		],
		review: [],
		skipped: [],
	});
	const queued = calls.find((c) => c.method.endsWith("queue_bulk_create"));
	assert.deepEqual(queued.args.orders, [
		{
			sales_order: "SO-1",
			receiver_name: "Ahmed",
			receiver_phone: "0100",
			city_id: "c-cairo",
			address_first_line: "12 Example Street",
			expected_cod: 150,
		},
	]);
});

test("a retry entry re-reads the server defaults and keeps the values already used", async () => {
	const fresh = {
		cod_amount: 175,
		city_id: "c-alex",
		city_name: "Alexandria",
		address_city: "Cairo",
		receiver_name: "Other",
		address_first_line: "changed",
	};
	const { module_exports, calls } = load({ defaults: fresh });
	const entry = await module_exports.retry_entry(USED);
	assert.equal(calls[0].args.sales_order, "SO-1");
	assert.equal(entry.defaults.cod_amount, 175);
	assert.equal(entry.defaults.address_city, "Cairo");
	// The city and the receiver stay as used, not as the server resolves them now.
	assert.equal(entry.defaults.city_id, "c-cairo");
	assert.equal(entry.defaults.receiver_name, "Ahmed");
	assert.equal(entry.defaults.receiver_phone, "0100");
	assert.equal(entry.defaults.address_first_line, "12 Example Street");
	assert.equal("district_id" in entry.defaults, false);
	assert.equal("district_candidates" in entry.defaults, false);
});

test("a retry entry falls back to the used values when the defaults cannot be read", async () => {
	const { module_exports } = load({ defaults_fail: true });
	const entry = await module_exports.retry_entry(USED);
	assert.deepEqual(entry.defaults, {
		receiver_name: "Ahmed",
		receiver_phone: "0100",
		city_id: "c-cairo",
		address_first_line: "12 Example Street",
		cod_amount: 150,
	});
});

test("onload attaches the order hover card to the list next to the bulk action", async () => {
	let erpnext_ran = false;
	const { settings, created_cards, attached } = load({ original_onload: () => (erpnext_ran = true) });
	const view = listview();
	settings.onload(view);
	await new Promise((resolve) => setTimeout(resolve, 5));
	assert.ok(erpnext_ran);
	assert.deepEqual(created_cards, [
		{ method: "marketplace_integrations.api.order_hover_card.get_sales_order_card", arg: "sales_order" },
	]);
	assert.deepEqual(attached, [view]);
});

test("users without a creator role still get the hover card", async () => {
	const { settings, attached } = load({ creator: false });
	settings.onload(listview());
	await new Promise((resolve) => setTimeout(resolve, 5));
	assert.equal(attached.length, 1);
});

test("a hover card that fails to load does not break the list", async () => {
	const { settings } = load();
	const view = listview();
	const original_error = console.error;
	const errors = [];
	console.error = (...args) => errors.push(args);
	const original_require = global.frappe.require;
	global.frappe.require = (asset) =>
		asset.endsWith("order_hover_card.js") ? Promise.reject(new Error("offline")) : original_require(asset);
	try {
		settings.onload(view);
		await new Promise((resolve) => setTimeout(resolve, 5));
	} finally {
		console.error = original_error;
	}
	assert.equal(errors.length, 1);
	// The bulk action does not depend on the card: it was still added.
	assert.deepEqual(view.items.map((item) => item.label), ["Create Bosta Shipments"]);
});

test("loading the list script again does not wrap onload a second time", async () => {
	let erpnext_runs = 0;
	const { settings, module_exports } = load({ original_onload: () => erpnext_runs++ });
	const wrapped = settings.onload;
	const path = require.resolve("./sales_order_list.js");
	delete require.cache[path];
	require(path);
	assert.equal(global.frappe.listview_settings["Sales Order"].onload, wrapped);
	const view = listview();
	settings.onload(view);
	await new Promise((resolve) => setTimeout(resolve, 5));
	assert.equal(erpnext_runs, 1);
	assert.equal(view.items.length, 1);
	assert.ok(module_exports);
});

test("Bosta delivery status renders as a translated, escaped color pill and preserves ERPNext formatters", () => {
	const existing = () => "existing";
	const { settings } = load({ original_formatters: { another_field: existing } });
	assert.equal(settings.formatters.another_field, existing);
	assert.equal(
		settings.formatters.custom_bosta_delivery_status("Delivered"),
		'<span class="indicator-pill green">Delivered</span>'
	);
	assert.equal(
		settings.formatters.custom_bosta_delivery_status("<unsafe>"),
		'<span class="indicator-pill grey">&lt;unsafe></span>'
	);
	assert.equal(settings.formatters.custom_bosta_delivery_status(""), "");
});

test("Delivery Status shows Noon's Shipped n/m translated with a color, and Bosta states as before", () => {
	const { settings } = load();
	const format = settings.formatters.custom_delivery_status;
	assert.equal(format("Shipped 2/3"), '<span class="indicator-pill blue">Shipped 2/3</span>');
	assert.equal(format("Shipped 3/3"), '<span class="indicator-pill green">Shipped 3/3</span>');
	assert.equal(format("Shipped 0/3"), '<span class="indicator-pill orange">Shipped 0/3</span>');
	assert.equal(format("Delivered"), '<span class="indicator-pill green">Delivered</span>');
	assert.equal(format(""), "");
	assert.equal(format("Shipped x/3"), '<span class="indicator-pill grey">Shipped x/3</span>');
});
