const assert = require("node:assert/strict");
const test = require("node:test");

global.__ = (message, args) => {
	if (!args) return message;
	return message.replace(/\{(\d+)\}/g, (_, index) => args[index]);
};

function load({ creator = true, original_onload = null, defaults = null, defaults_fail = false } = {}) {
	const msgprints = [];
	const calls = [];
	const dialogs = [];
	global.frappe = {
		user: { has_role: () => creator },
		utils: { escape_html: (value) => String(value).replace(/</g, "&lt;") },
		listview_settings: { "Sales Order": original_onload ? { onload: original_onload } : {} },
		msgprint: (message) => msgprints.push(message),
		// Loading sales_order.js is what exports the creator roles.
		require: () => {
			global.frappe.marketplace_integrations = { BOSTA_CREATOR_ROLES: ["Sales Manager"] };
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
	return { calls, msgprints, dialogs, module_exports, settings: global.frappe.listview_settings["Sales Order"] };
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
	city: "Cairo",
	district_id: "d-1",
	address_first_line: "12 Example Street",
	expected_cod: 150,
};

test("a retry entry re-reads the server defaults and keeps the values already used", async () => {
	const fresh = { cod_amount: 175, city_id: "c-cairo", city_name: "Cairo", district_candidates: ["d-1", "d-2"], receiver_name: "Other", address_first_line: "changed" };
	const { module_exports, calls } = load({ defaults: fresh });
	const entry = await module_exports.retry_entry(USED);
	assert.equal(calls[0].args.sales_order, "SO-1");
	assert.equal(entry.defaults.cod_amount, 175);
	assert.equal(entry.defaults.city_id, "c-cairo");
	assert.deepEqual(entry.defaults.district_candidates, ["d-1", "d-2", "d-1"]);
	assert.equal(entry.defaults.district_id, "d-1");
	assert.equal(entry.defaults.receiver_name, "Ahmed");
	assert.equal(entry.defaults.receiver_phone, "0100");
	assert.equal(entry.defaults.address_first_line, "12 Example Street");
});

test("a retry entry drops the resolved city when it is not the city used", async () => {
	const { module_exports } = load({ defaults: { cod_amount: 175, city_id: "c-giza", city_name: "Giza", district_candidates: ["g-1"] } });
	const entry = await module_exports.retry_entry(USED);
	assert.equal(entry.defaults.city_id, undefined);
	assert.equal(entry.defaults.district_candidates, undefined);
	assert.equal(entry.defaults.address_city, "Cairo");
	assert.equal(entry.defaults.district_id, "d-1");
});

test("a retry entry falls back to the used values when the defaults cannot be read", async () => {
	const { module_exports } = load({ defaults_fail: true });
	const entry = await module_exports.retry_entry(USED);
	assert.deepEqual(entry.defaults, {
		receiver_name: "Ahmed",
		receiver_phone: "0100",
		address_city: "Cairo",
		district_id: "d-1",
		address_first_line: "12 Example Street",
		cod_amount: 150,
	});
});
