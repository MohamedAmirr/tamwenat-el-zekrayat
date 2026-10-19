const assert = require("node:assert/strict");
const test = require("node:test");

global.__ = (message, args) => {
	if (!args) return message;
	return message.replace(/\{(\d+)\}/g, (_, index) => args[index]);
};

function load({ creator = true, original_onload = null } = {}) {
	const msgprints = [];
	const calls = [];
	const dialogs = [];
	global.frappe = {
		user: { has_role: () => creator },
		utils: { escape_html: (value) => String(value).replace(/</g, "&lt;") },
		listview_settings: { "Sales Order": original_onload ? { onload: original_onload } : {} },
		msgprint: (message) => msgprints.push(message),
		require: () => Promise.resolve(),
		call: (opts) => {
			calls.push(opts);
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

test("creators get the bulk action and ERPNext's own onload still runs", () => {
	let erpnext_ran = false;
	const { settings } = load({ original_onload: () => (erpnext_ran = true) });
	const view = listview();
	settings.onload(view);
	assert.ok(erpnext_ran);
	assert.deepEqual(view.items.map((item) => item.label), ["Create Bosta Shipments"]);
});

test("users without a creator role do not get the action", () => {
	const { settings } = load({ creator: false });
	const view = listview();
	settings.onload(view);
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

test("a retry entry reopens the dialog with the values already used", () => {
	const { module_exports } = load();
	const entry = module_exports.retry_entry({
		sales_order: "SO-1",
		receiver_name: "Ahmed",
		receiver_phone: "0100",
		city: "Cairo",
		district_id: "d-1",
		address_first_line: "12 Example Street",
		expected_cod: 150,
	});
	assert.equal(entry.sales_order, "SO-1");
	assert.deepEqual(entry.defaults, {
		receiver_name: "Ahmed",
		receiver_phone: "0100",
		address_city: "Cairo",
		district_id: "d-1",
		address_first_line: "12 Example Street",
		cod_amount: 150,
	});
});
