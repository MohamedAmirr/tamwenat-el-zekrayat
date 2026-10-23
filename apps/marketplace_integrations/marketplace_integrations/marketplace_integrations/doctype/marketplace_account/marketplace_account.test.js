const assert = require("node:assert/strict");
const test = require("node:test");

global.__ = (message) => message;

const {
	TALABAT_OUTLET_CLOSE_REASONS,
	TALABAT_OUTLET_FIELDS,
	SHOPIFY_LOCATION_FIELDS,
	PLATFORM_CONNECTION_DOCTYPES,
	build_outlet_status_args,
	outlet_actions_for_status,
	should_show_outlet_management,
	should_show_shopify_location,
	set_default_connection_doctype,
	update_connection_doctype_on_platform_change,
	clear_fields_for_platform_left,
	configure_outlet_management,
	configure_shopify_location,
	configure_shopify_inventory,
	reconcile_shopify_inventory,
	RECONCILE_INVENTORY_LABEL,
	LOAD_SHOPIFY_GATEWAYS_LABEL,
	show_shopify_gateway_picker,
	configure_view_shortcuts,
} = require("./marketplace_account.js");

function make_frm(doc, { is_new = false } = {}) {
	// Minimal frm stand-in shared by the tests below - set_value mirrors the
	// real frm.set_value() closely enough for these pure-logic tests: it
	// writes through to frm.doc so assertions can check frm.doc directly,
	// same as the real thing does after a round-trip. add_custom_button/
	// remove_custom_button track button labels in a plain array so tests can
	// assert on exactly what's currently "on the form".
	const buttons = [];
	return {
		doc,
		buttons,
		refresh_field: () => {},
		toggle_display: () => {},
		set_df_property: () => {},
		is_new: () => is_new,
		set_value(fieldname, value) {
			this.doc[fieldname] = value;
		},
		add_custom_button(label, action, group) {
			buttons.push(label);
			if (action) this.button_actions = { ...this.button_actions, [`${group || ""}:${label}`]: action };
		},
		remove_custom_button(label) {
			const index = buttons.indexOf(label);
			if (index !== -1) buttons.splice(index, 1);
		},
	};
}

test("outlet management is visible only for Talabat accounts", () => {
	assert.equal(should_show_outlet_management("Talabat"), true);
	assert.equal(should_show_outlet_management("Shopify"), false);
	assert.equal(should_show_outlet_management("Noon"), false);
});

test("Shopify Location section is visible only for Shopify accounts", () => {
	assert.equal(should_show_shopify_location("Shopify"), true);
	assert.equal(should_show_shopify_location("Talabat"), false);
	assert.equal(should_show_shopify_location("Noon"), false);
});

test("Talabat outlet actions are driven only by the last confirmed status", () => {
	assert.deepEqual(outlet_actions_for_status("OPEN"), ["close_today", "close_until"]);
	assert.deepEqual(outlet_actions_for_status("CLOSED"), ["open", "close_today", "close_until"]);
	assert.deepEqual(outlet_actions_for_status("CLOSED_TODAY"), ["open", "close_today", "close_until"]);
	assert.deepEqual(outlet_actions_for_status("CLOSED_UNTIL"), ["open", "close_today", "close_until"]);
	assert.deepEqual(outlet_actions_for_status("CHECKIN"), []);
	assert.deepEqual(outlet_actions_for_status(undefined), []);
});

test("Talabat outlet dialogs expose only documented close reasons", () => {
	assert.deepEqual(TALABAT_OUTLET_CLOSE_REASONS, [
		"TOO_BUSY_NO_DRIVERS",
		"TOO_BUSY_KITCHEN",
		"UPDATES_IN_MENU",
		"TECHNICAL_PROBLEM",
		"CLOSED",
		"OTHER",
		"BAD_WEATHER",
		"HOLIDAY_SPECIAL_DAY",
	]);
});

test("setting platform on a new document defaults connection_doctype when blank", () => {
	const frm = { doc: { platform: "Shopify", connection_doctype: "" }, refresh_field: () => {} };
	set_default_connection_doctype(frm);
	assert.equal(frm.doc.connection_doctype, PLATFORM_CONNECTION_DOCTYPES.Shopify);
});

test("Noon defaults its own connection doctype on a new document", () => {
	const frm = { doc: { platform: "Noon", connection_doctype: "" }, refresh_field: () => {} };
	set_default_connection_doctype(frm);
	assert.equal(frm.doc.connection_doctype, "Noon Connection");
});

test("changing platform does not overwrite an already-set connection_doctype", () => {
	const frm = { doc: { platform: "Shopify", connection_doctype: "Talabat Connection" }, refresh_field: () => {} };
	set_default_connection_doctype(frm);
	assert.equal(frm.doc.connection_doctype, "Talabat Connection");
});

test("changing platform on an existing document overrides a stale connection_doctype (#103 review finding #4)", () => {
	const frm = make_frm({ platform: "Shopify", connection_doctype: "Talabat Connection" });
	update_connection_doctype_on_platform_change(frm);
	assert.equal(frm.doc.connection_doctype, "Shopify Connection");

	frm.doc.platform = "Talabat";
	update_connection_doctype_on_platform_change(frm);
	assert.equal(frm.doc.connection_doctype, "Talabat Connection");
});

test("a real platform change clears the old connection and the fields specific to the platform being left (#103 review finding #5)", () => {
	const frm = make_frm({
		platform: "Shopify",
		connection_doctype: "Talabat Connection",
		connection: "Some Talabat Connection",
		external_status: "OPEN",
		external_status_reason: "stale",
		shopify_location_gid: null,
		shopify_location_name: null,
	});
	update_connection_doctype_on_platform_change(frm);

	assert.equal(frm.doc.connection_doctype, "Shopify Connection");
	assert.equal(frm.doc.connection, "");
	// Leaving Talabat -> Talabat outlet-status fields are cleared.
	for (const fieldname of TALABAT_OUTLET_FIELDS) {
		if (fieldname === "outlet_status_section") continue;
		assert.equal(frm.doc[fieldname], null);
	}

	frm.doc.platform = "Talabat";
	frm.doc.connection = "Some Shopify Connection";
	frm.doc.shopify_location_gid = "gid://shopify/Location/1";
	frm.doc.shopify_location_name = "Test Location";
	update_connection_doctype_on_platform_change(frm);

	assert.equal(frm.doc.connection_doctype, "Talabat Connection");
	assert.equal(frm.doc.connection, "");
	// Leaving Shopify -> Shopify Location fields are cleared.
	for (const fieldname of SHOPIFY_LOCATION_FIELDS) {
		if (fieldname === "shopify_location_section") continue;
		assert.equal(frm.doc[fieldname], null);
	}
});

test("clear_fields_for_platform_left clears Talabat fields when leaving Talabat and Shopify fields when leaving Shopify", () => {
	const leaving_talabat = make_frm({
		platform: "Shopify",
		external_status: "OPEN",
		external_status_reason: "stale",
	});
	clear_fields_for_platform_left(leaving_talabat);
	assert.equal(leaving_talabat.doc.external_status, null);
	assert.equal(leaving_talabat.doc.external_status_reason, null);

	const leaving_shopify = make_frm({
		platform: "Talabat",
		shopify_location_gid: "gid://shopify/Location/1",
		shopify_location_name: "Test Location",
	});
	clear_fields_for_platform_left(leaving_shopify);
	assert.equal(leaving_shopify.doc.shopify_location_gid, null);
	assert.equal(leaving_shopify.doc.shopify_location_name, null);
});

test("switching to Noon clears the old connection and all Talabat and Shopify state", () => {
	for (const old_connection_doctype of ["Talabat Connection", "Shopify Connection"]) {
		const doc = {
			platform: "Noon",
			connection_doctype: old_connection_doctype,
			connection: "Old connection",
			external_account_id: "Old platform ID",
			external_status_payload: '{"status":"OPEN"}',
			shopify_orders_reconciled_through: "2026-09-29 09:00:00",
		};
		for (const fieldname of [...TALABAT_OUTLET_FIELDS, ...SHOPIFY_LOCATION_FIELDS]) {
			if (!fieldname.endsWith("_section")) doc[fieldname] = "stale";
		}
		const frm = make_frm(doc);
		update_connection_doctype_on_platform_change(frm);

		assert.equal(frm.doc.connection_doctype, "Noon Connection");
		assert.equal(frm.doc.connection, "");
		for (const fieldname of [
			...TALABAT_OUTLET_FIELDS,
			"external_status_payload",
			...SHOPIFY_LOCATION_FIELDS,
			"shopify_orders_reconciled_through",
			"external_account_id",
		]) {
			if (!fieldname.endsWith("_section")) assert.equal(frm.doc[fieldname], null);
		}
	}
});

test("Talabat outlet buttons are removed when platform is switched away from Talabat (#103 review finding #3)", () => {
	const frm = make_frm({ platform: "Talabat", external_status: "OPEN" }, { is_new: false });
	configure_outlet_management(frm);
	assert.ok(frm.buttons.includes("Refresh Status"));
	assert.ok(frm.buttons.includes("Close Today"));
	assert.ok(frm.buttons.includes("Close Until"));

	frm.doc.platform = "Shopify";
	configure_outlet_management(frm);

	assert.deepEqual(frm.buttons, []);
});

test("Load Shopify Locations button is removed when connection is cleared, even while platform stays Shopify (#103 review finding #4)", () => {
	const frm = make_frm({ platform: "Shopify", connection: "Some Shopify Connection" });
	configure_shopify_location(frm);
	assert.ok(frm.buttons.includes("Load Shopify Locations"));

	frm.doc.connection = "";
	configure_shopify_location(frm);

	assert.deepEqual(frm.buttons, []);
});

test("Load Shopify Payment Gateways is Shopify-only, permission-gated, and saved-account-only", () => {
	global.frappe = { model: { can_write: () => true } };
	const frm = make_frm({ platform: "Shopify", connection: "Shopify Connection", name: "MKTACC-1" });
	configure_shopify_location(frm);
	assert.ok(frm.buttons.includes(LOAD_SHOPIFY_GATEWAYS_LABEL));

	const new_frm = make_frm({ platform: "Shopify", connection: "Shopify Connection" }, { is_new: true });
	configure_shopify_location(new_frm);
	assert.ok(!new_frm.buttons.includes(LOAD_SHOPIFY_GATEWAYS_LABEL));

	global.frappe.model.can_write = () => false;
	const read_only_frm = make_frm({ platform: "Shopify", connection: "Shopify Connection", name: "MKTACC-2" });
	configure_shopify_location(read_only_frm);
	assert.ok(!read_only_frm.buttons.includes(LOAD_SHOPIFY_GATEWAYS_LABEL));
});

test("gateway picker adds selected exact unmapped names without saving or showing formatted labels", async () => {
	let dialog_options;
	let selected_indices = [1];
	global.frappe = {
		call: () =>
			Promise.resolve({
				message: [
					{ gateway: "Kashier", orders_count: 2, mapped: true },
					{ gateway: "Paymob", orders_count: 3, mapped: false, formatted_gateway: "Pretty label" },
					{ gateway: "<script>", orders_count: 1, mapped: false },
				],
		}),
		utils: { escape_html: (value) => String(value).replace(/</g, "&lt;").replace(/>/g, "&gt;") },
		msgprint() {},
		ui: {
			Dialog: class {
				constructor(options) {
					dialog_options = options;
					this.$wrapper = {
						find: () => ({
							map: (callback) => ({
								get: () =>
									selected_indices
										.map((index) => callback(index, { dataset: { gatewayIndex: String(index) } }))
										.filter(Boolean),
							}),
						}),
					};
				}
				show() {}
				hide() {}
			},
		},
	};
	const frm = make_frm({ name: "MKTACC-1", shopify_payment_gateways: [] });
	frm.add_child = (fieldname, value) => frm.doc[fieldname].push(value);
	let refreshed = false;
	frm.refresh_field = (fieldname) => {
		if (fieldname === "shopify_payment_gateways") refreshed = true;
	};
	await show_shopify_gateway_picker(frm);
	const html = dialog_options.fields[0].options;
	assert.match(html, /Kashier/);
	assert.match(html, /Already mapped/);
	assert.match(html, /Used by/);
	assert.doesNotMatch(html, /Pretty label/);
	assert.match(html, /&lt;script&gt;/);
	dialog_options.primary_action();
	assert.deepEqual(frm.doc.shopify_payment_gateways, [{ shopify_gateway: "Paymob" }]);
	assert.equal(refreshed, true);
	assert.equal(typeof frm.save, "undefined");
});

test("Open sends no close fields and Close Until preserves the local DateTime value", () => {
	assert.deepEqual(build_outlet_status_args("OPEN"), { status: "OPEN" });
	assert.deepEqual(build_outlet_status_args("CLOSED_TODAY", "OTHER"), {
		status: "CLOSED_TODAY",
		closed_reason: "OTHER",
	});
	assert.deepEqual(build_outlet_status_args("CLOSED_UNTIL", "OTHER", "2026-09-02 15:30:00"), {
		status: "CLOSED_UNTIL",
		closed_reason: "OTHER",
		closed_until: "2026-09-02 15:30:00",
	});
});

test("Reconcile Shopify Inventory appears only on a saved, enabled Shopify account with write permission", () => {
	global.frappe = { model: { can_write: () => true } };
	let frm = make_frm({ platform: "Shopify", enabled: 1 });
	configure_shopify_inventory(frm);
	assert.deepEqual(frm.buttons, [RECONCILE_INVENTORY_LABEL]);

	frm = make_frm({ platform: "Shopify", enabled: 1 }, { is_new: true });
	configure_shopify_inventory(frm);
	assert.deepEqual(frm.buttons, []);

	frm = make_frm({ platform: "Shopify", enabled: 0 });
	configure_shopify_inventory(frm);
	assert.deepEqual(frm.buttons, []);

	frm = make_frm({ platform: "Talabat", enabled: 1 });
	configure_shopify_inventory(frm);
	assert.deepEqual(frm.buttons, []);

	global.frappe = { model: { can_write: () => false } };
	frm = make_frm({ platform: "Shopify", enabled: 1 });
	configure_shopify_inventory(frm);
	assert.deepEqual(frm.buttons, []);
});

test("Reconcile Shopify Inventory button is removed when the platform changes away from Shopify", () => {
	global.frappe = { model: { can_write: () => true } };
	const frm = make_frm({ platform: "Shopify", enabled: 1 });
	configure_shopify_inventory(frm);
	assert.equal(frm.buttons.length, 1);

	frm.doc.platform = "Talabat";
	configure_shopify_inventory(frm);

	assert.deepEqual(frm.buttons, []);
});

test("reconcile_shopify_inventory confirms, queues the account and opens the inventory dialog", async () => {
	let call_options;
	let dialog_args;
	global.frappe = {
		confirm(_message, on_yes) {
			on_yes();
		},
		call(options) {
			call_options = options;
			return Promise.resolve({
				message: { jobs: [{ account: "MKTACC-1", job_id: "job-5", mapping_count: null }] },
			});
		},
		require: () => Promise.resolve(),
		ui: {
			ShopifyCatalogPushStatus: class {
				static INVENTORY_OPTIONS = { status_method: "inventory.status" };
				constructor(args) {
					dialog_args = args;
				}
			},
		},
	};

	reconcile_shopify_inventory({ doc: { name: "MKTACC-1" } });
	for (let i = 0; i < 4; i++) await new Promise((resolve) => setImmediate(resolve));

	assert.equal(
		call_options.method,
		"marketplace_integrations.api.shopify_inventory.queue_account_reconciliation"
	);
	assert.deepEqual(call_options.args, { account: "MKTACC-1" });
	assert.equal(dialog_args.status_method, "inventory.status");
});

test("reconcile_shopify_inventory makes no call when the confirmation is declined", () => {
	let called = false;
	global.frappe = {
		confirm() {},
		call() {
			called = true;
		},
	};

	reconcile_shopify_inventory({ doc: { name: "MKTACC-1" } });

	assert.equal(called, false);
});

test("View button group routes to the Marketplace Order/Product Mapping/Sync Log lists filtered by this account", () => {
	const set_route_calls = [];
	global.frappe = { set_route: (...args) => set_route_calls.push(args) };

	const frm = make_frm({ name: "MKTACC-42" });
	configure_view_shortcuts(frm);

	assert.deepEqual(frm.buttons, ["Orders", "Product Mappings", "Sync Log"]);

	frm.button_actions["View:Orders"]();
	frm.button_actions["View:Product Mappings"]();
	frm.button_actions["View:Sync Log"]();

	assert.deepEqual(set_route_calls, [
		["List", "Marketplace Order", { marketplace_account: "MKTACC-42" }],
		["List", "Marketplace Product Mapping", { marketplace_account: "MKTACC-42" }],
		["List", "Marketplace Sync Log", { marketplace_account: "MKTACC-42" }],
	]);
});

test("View button group is not shown on an unsaved (new) Marketplace Account", () => {
	const frm = make_frm({ name: "new-marketplace-account-1" }, { is_new: true });
	configure_view_shortcuts(frm);
	assert.deepEqual(frm.buttons, []);
});

test("reconcile_shopify_inventory shows the skipped account's real reason, escaped", async () => {
	let msgprint_args;
	global.frappe = {
		confirm(_message, on_yes) {
			on_yes();
		},
		call() {
			return Promise.resolve({
				message: { jobs: [], skipped_accounts: [{ account: "MKTACC-1", reason: "Warehouse <b>x</b>" }] },
			});
		},
		msgprint(args) {
			msgprint_args = args;
		},
		utils: { escape_html: (text) => text.replace(/</g, "&lt;") },
	};

	reconcile_shopify_inventory({ doc: { name: "MKTACC-1" } });
	for (let i = 0; i < 4; i++) await new Promise((resolve) => setImmediate(resolve));

	assert.match(msgprint_args.message, /Warehouse &lt;b>x&lt;\/b>/);
});
