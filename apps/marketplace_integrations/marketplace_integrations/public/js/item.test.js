const assert = require("node:assert/strict");
const test = require("node:test");

global.__ = (message, values) =>
	values ? values.reduce((text, value, index) => text.replace(`{${index}}`, value), message) : message;

const sync_module = require("./mapping_sync_actions.js");
const item = require("./item.js");

const LISTINGS = "marketplace_integrations.api.item_listings.get_item_listings";

function escape_html(value) {
	return String(value).replace(/&/g, "&amp;").replace(/</g, "&lt;").replace(/>/g, "&gt;").replace(/"/g, "&quot;");
}

// Every test installs its own global frappe mock; the shared sync module is always attached.
function install_frappe(overrides = {}) {
	const dialogs = [];
	global.frappe = Object.assign(
		{
			utils: { escape_html },
			marketplace_integrations: { mapping_sync: sync_module },
			model: { can_read: () => true, can_write: () => true, can_create: () => true },
			datetime: { comment_when: (value) => `<span class="when">${value}</span>` },
			msgprint() {},
			require: () => Promise.resolve(),
			set_route() {},
			new_doc() {},
			confirm: (message, yes) => yes(),
			ui: {
				ShopifyCatalogPushStatus: class {
					static INVENTORY_OPTIONS = { status_method: "inventory.status" };
					constructor(args) {
						this.args = args;
						dialogs.push(args);
					}
				},
			},
		},
		overrides
	);
	global.frappe.__dialogs = dialogs;
	return global.frappe;
}

function make_row(overrides = {}) {
	return {
		name: "MPM-1",
		marketplace_account: "MKTACC-1",
		account_name: "Shop A",
		external_reference: "REF-1",
		platform: "Shopify",
		enabled: 1,
		sales_uom: "Nos",
		sync_status: "Synced",
		last_synced_on: "2026-11-01 10:00:00",
		last_error: "",
		inventory_sync_status: "Synced",
		inventory_last_synced_on: "2026-11-01 11:00:00",
		inventory_last_error: "",
		can_write: true,
		price_push_off: false,
		stock_push_off: false,
		...overrides,
	};
}

function make_frm({ is_new = false } = {}) {
	const wrapper = {
		html(markup) {
			this.last = markup;
			return this;
		},
		last: "",
	};
	return {
		doc: { name: "ITEM-1" },
		is_new: () => is_new,
		toggled: {},
		toggle_display(fieldname, visible) {
			this.toggled[fieldname] = visible;
		},
		get_field: () => ({ $wrapper: wrapper }),
		wrapper,
	};
}

async function settle() {
	for (let i = 0; i < 8; i++) {
		await new Promise((resolve) => setImmediate(resolve));
	}
}

function calls_handler(rows, extra = {}) {
	const calls = [];
	const call = (options) => {
		calls.push(options);
		if (options.method === LISTINGS) return Promise.resolve({ message: rows });
		if (extra[options.method]) return extra[options.method](options);
		throw new Error(`Unexpected frappe.call in test: ${options.method}`);
	};
	return { calls, call };
}

async function loaded_frm(rows, extra, frappe_overrides = {}) {
	const { calls, call } = calls_handler(rows, extra);
	install_frappe({ call, ...frappe_overrides });
	const frm = make_frm();
	await item.render_listings_tab(frm);
	return { frm, calls };
}

test("an unsaved Item hides the tab and makes no request", async () => {
	install_frappe({
		call() {
			throw new Error("no request expected");
		},
	});
	const frm = make_frm({ is_new: true });

	await item.render_listings_tab(frm);

	assert.deepEqual(frm.toggled, { custom_marketplace_listings_tab: false, custom_marketplace_listings_html: false });
	assert.equal(frm.wrapper.last, "");
});

test("without read on the mapping the tab is hidden and no request is made", async () => {
	install_frappe({
		model: { can_read: () => false, can_write: () => true, can_create: () => true },
		call() {
			throw new Error("no request expected");
		},
	});
	const frm = make_frm();

	await item.render_listings_tab(frm);

	assert.equal(frm.toggled.custom_marketplace_listings_tab, false);
});

test("the table lists the columns in the specified order, with references in a ltr bdi", async () => {
	const { frm, calls } = await loaded_frm([
		make_row(),
		make_row({ name: "MPM-2", platform: "Talabat", account_name: "Talabat A", external_reference: "T-9" }),
	]);

	assert.equal(frm.toggled.custom_marketplace_listings_tab, true);
	assert.equal(calls[0].type, "GET");
	assert.deepEqual(calls[0].args, { item: "ITEM-1" });
	const html = frm.wrapper.last;
	const order = [
		"Marketplace Account",
		"External Reference",
		"Platform",
		"Enabled",
		"Sales UOM",
		"Sync Status",
		"Inventory Sync Status",
	].map((label) => html.indexOf(`>${label}<`));
	assert.ok(order.every((position) => position >= 0), "every column heading renders");
	assert.deepEqual(order, [...order].sort((a, b) => a - b));
	assert.match(html, /<bdi dir="ltr">REF-1<\/bdi>/);
	assert.match(html, /badge[^>]*>Shopify</);
	assert.match(html, /grid-heading-row/);
	assert.match(html, /indicator-pill green/);
});

test("Talabat shows a dash for the inventory sync status", async () => {
	const { frm } = await loaded_frm([make_row({ platform: "Talabat", inventory_sync_status: "Error" })]);

	assert.doesNotMatch(frm.wrapper.last, /indicator-pill red/);
});

test("values containing angle brackets render as text", async () => {
	const evil = '<img src=x onerror="alert(1)">';
	const { frm } = await loaded_frm([
		make_row({ external_reference: evil, account_name: evil, sales_uom: evil, last_error: evil, inventory_last_error: evil }),
	]);
	item.on_action(frm, "expand", { name: "MPM-1" });

	assert.doesNotMatch(frm.wrapper.last, /<img/);
	assert.match(frm.wrapper.last, /&lt;img src=x onerror=&quot;alert\(1\)&quot;&gt;/);
});

test("expanding a row shows last synced and last error values, for inventory too", async () => {
	const { frm } = await loaded_frm([make_row({ last_error: "Price list missing", inventory_last_error: "Location gone" })]);
	assert.doesNotMatch(frm.wrapper.last, /Price list missing/);

	item.on_action(frm, "expand", { name: "MPM-1" });

	const html = frm.wrapper.last;
	assert.match(html, /Last Synced On/);
	assert.match(html, /class="when">2026-11-01 10:00:00/);
	assert.match(html, /Last Error:<\/span> <span[^>]*>Price list missing/);
	assert.match(html, /Inventory Last Synced On/);
	assert.match(html, /Inventory Last Error:<\/span> <span[^>]*>Location gone/);

	item.on_action(frm, "expand", { name: "MPM-1" });
	assert.doesNotMatch(frm.wrapper.last, /Price list missing/);
});

test("the Talabat detail has no inventory lines", async () => {
	const { frm } = await loaded_frm([make_row({ platform: "Talabat" })]);
	item.on_action(frm, "expand", { name: "MPM-1" });

	assert.match(frm.wrapper.last, /Last Synced On/);
	assert.doesNotMatch(frm.wrapper.last, /Inventory Last Synced On/);
});

test("an empty Item shows the empty text, with Add Listing only when the user can create", async () => {
	const { frm } = await loaded_frm([]);
	assert.match(frm.wrapper.last, /No marketplace listings for this Item/);
	assert.match(frm.wrapper.last, /data-action="add"/);

	const { frm: read_only } = await loaded_frm([], {}, {
		model: { can_read: () => true, can_write: () => false, can_create: () => false },
	});
	assert.match(read_only.wrapper.last, /No marketplace listings for this Item/);
	assert.doesNotMatch(read_only.wrapper.last, /data-action="add"/);
});

test("a failed load shows the escaped server message and a Retry action", async () => {
	install_frappe({
		call() {
			return Promise.reject({
				responseJSON: { _server_messages: JSON.stringify([JSON.stringify({ message: "Nope &lt;b&gt;bad&lt;/b&gt; <i>x</i>" })]) },
			});
		},
	});
	const frm = make_frm();

	await item.render_listings_tab(frm);

	// The shared parser decodes entities and drops markup; the render escapes what is left.
	assert.match(frm.wrapper.last, /Nope bad x/);
	assert.doesNotMatch(frm.wrapper.last, /<b>|<i>/);
	assert.match(frm.wrapper.last, /data-action="retry"/);
});

test("Add Listing opens a new mapping with the item filled in; View all filters the list by item", async () => {
	const new_docs = [];
	const routes = [];
	const { frm } = await loaded_frm([make_row()], {}, {
		new_doc: (doctype, values) => new_docs.push([doctype, values]),
		set_route: (...args) => routes.push(args),
	});

	item.on_action(frm, "add", {});
	item.on_action(frm, "view_all", {});
	item.open_row("MPM-1");

	assert.deepEqual(new_docs, [["Marketplace Product Mapping", { item: "ITEM-1" }]]);
	assert.deepEqual(routes, [
		["List", "Marketplace Product Mapping", { item: "ITEM-1" }],
		["Form", "Marketplace Product Mapping", "MPM-1"],
	]);
});

test("the sync controls are absent without write on the mapping", async () => {
	const { frm } = await loaded_frm([make_row()], {}, {
		model: { can_read: () => true, can_write: () => false, can_create: () => false },
	});

	assert.doesNotMatch(frm.wrapper.last, /data-action="sync"/);
});

test("a sync click with the shared helper missing shows the reload error and calls nothing", async () => {
	const shown = [];
	// frappe.require() resolves even when the script failed to load, so the module is simply absent.
	const { frm, calls } = await loaded_frm([make_row()], {}, {
		marketplace_integrations: {},
		msgprint: (args) => shown.push(args),
	});
	const calls_before = calls.length;

	item.start_sync(frm, "row", "MPM-1", "both");
	await settle();

	assert.equal(calls.length, calls_before);
	assert.equal(shown.length, 1);
	assert.equal(shown[0].indicator, "red");
	assert.equal(shown[0].message, "Could not load the sync actions. Reload the page and try again.");
});

test("row_sync_state gives each disabled control its reason", () => {
	install_frappe();
	const base = make_row();

	assert.equal(item.row_sync_state(make_row({ platform: "Bosta" }), "both", true).reason, "This platform has no manual sync.");
	assert.equal(item.row_sync_state(base, "both", false).reason, "You do not have permission to sync this listing.");
	assert.equal(
		item.row_sync_state(make_row({ can_write: false }), "both", true).reason,
		"You do not have permission to sync this listing."
	);
	const talabat = make_row({ platform: "Talabat" });
	for (const kind of ["price", "stock"]) {
		assert.equal(
			item.row_sync_state(talabat, kind, true).reason,
			"Talabat updates price and stock together. Use Sync price and stock."
		);
	}
	assert.equal(item.row_sync_state(make_row({ enabled: 0 }), "both", true).reason, "This listing is disabled.");
	assert.equal(item.row_sync_state(make_row({ platform: "Noon", enabled: 0 }), "stock", true).reason, "This listing is disabled.");
	assert.deepEqual(item.row_sync_state(base, "both", true), { allowed: true, reason: "", talabat_inactive: false });
	// A disabled Talabat listing is allowed and flagged for the inactive / stock 0 confirm.
	assert.equal(item.row_sync_state(make_row({ platform: "Talabat", enabled: 0 }), "both", true).talabat_inactive, true);
});

test("a disabled sync control renders its reason as tooltip and aria-label", async () => {
	const { frm } = await loaded_frm([make_row({ platform: "Talabat" })]);

	const reason = "Talabat updates price and stock together. Use Sync price and stock.";
	assert.match(frm.wrapper.last, new RegExp(`data-kind="price"[^>]*title="${reason}" aria-label="${reason}"`));
	assert.match(frm.wrapper.last, new RegExp(`data-kind="stock"[^>]*title="${reason}"`));
});

test("a per-row Shopify sync confirms, then calls the price and stock endpoints and opens their dialogs in turn", async () => {
	const confirms = [];
	const alerts = [];
	const { frm, calls } = await loaded_frm(
		[make_row()],
		{
			"marketplace_integrations.api.shopify_catalog_push.queue_catalog_push": () =>
				Promise.resolve({ message: { jobs: [{ account: "MKTACC-1", job_id: "price-1" }] } }),
			"marketplace_integrations.api.shopify_inventory.queue_inventory_sync": () =>
				Promise.resolve({ message: { jobs: [{ account: "MKTACC-1", job_id: "stock-1" }] } }),
		},
		{
			confirm: (message, yes) => {
				confirms.push(message);
				yes();
			},
			msgprint: (args) => alerts.push(args),
		}
	);
	const listing_calls_before = calls.filter((c) => c.method === LISTINGS).length;

	item.start_sync(frm, "row", "MPM-1", "both");
	await settle();

	assert.match(confirms[0], /Send the current price and stock of <bdi dir="ltr">REF-1<\/bdi> to Shop A\?/);
	const price = calls.find((c) => c.method.endsWith("shopify_catalog_push.queue_catalog_push"));
	const stock = calls.find((c) => c.method.endsWith("shopify_inventory.queue_inventory_sync"));
	assert.deepEqual(price.args, { mapping_names: ["MPM-1"] });
	assert.deepEqual(stock.args, { mapping_names: ["MPM-1"] });
	assert.notEqual(price.freeze, true, "the form stays usable");
	assert.equal(calls.filter((c) => c.method === LISTINGS).length, listing_calls_before + 1, "the tab re-fetches");

	const dialogs = global.frappe.__dialogs;
	assert.equal(dialogs.length, 1, "one status dialog at a time");
	assert.deepEqual(dialogs[0].jobs, [{ account: "MKTACC-1", job_id: "price-1" }]);
	dialogs[0].on_done();
	await settle();
	assert.equal(dialogs.length, 2);
	assert.equal(dialogs[1].status_method, "inventory.status");
	assert.equal(alerts.length, 0, "the summary waits for the last dialog");

	dialogs[1].on_close();
	await settle();
	assert.equal(alerts.length, 1);
	assert.match(alerts[0].message, /Queued/);
	assert.equal(calls.filter((c) => c.method === LISTINGS).length, listing_calls_before + 2, "re-fetched after the dialogs closed");
});

test("Price only and Stock only on Shopify call just their own endpoint", async () => {
	const handler = (job) => () => Promise.resolve({ message: { jobs: [{ account: "A", job_id: job }] } });
	const { frm, calls } = await loaded_frm([make_row()], {
		"marketplace_integrations.api.shopify_catalog_push.queue_catalog_push": handler("p"),
		"marketplace_integrations.api.shopify_inventory.queue_inventory_sync": handler("s"),
	});

	item.start_sync(frm, "row", "MPM-1", "price");
	await settle();
	assert.equal(calls.filter((c) => c.method.endsWith("queue_catalog_push")).length, 1);
	assert.equal(calls.filter((c) => c.method.endsWith("queue_inventory_sync")).length, 0);
	global.frappe.__dialogs[0].on_close();
	await settle();

	item.start_sync(frm, "row", "MPM-1", "stock");
	await settle();
	assert.equal(calls.filter((c) => c.method.endsWith("queue_catalog_push")).length, 1);
	assert.equal(calls.filter((c) => c.method.endsWith("queue_inventory_sync")).length, 1);
});

test("Noon makes one call per account with the matching endpoint for each kind", async () => {
	const noon_reply = () => Promise.resolve({ message: { queued: true } });
	const both_reply = () => Promise.resolve({ message: { pricing: { queued: true }, stock: { queued: true } } });
	const rows = [
		make_row({ name: "N-1", platform: "Noon", marketplace_account: "NOON-A" }),
		make_row({ name: "N-2", platform: "Noon", marketplace_account: "NOON-A" }),
		make_row({ name: "N-3", platform: "Noon", marketplace_account: "NOON-B" }),
	];
	const { frm, calls } = await loaded_frm(rows, {
		"marketplace_integrations.api.noon_pricing.queue_pricing_push": noon_reply,
		"marketplace_integrations.api.noon_inventory.queue_stock_push": noon_reply,
		"marketplace_integrations.api.noon_catalog.queue_account_sync": both_reply,
	});

	item.start_sync(frm, "all", "", "both");
	await settle();
	item.start_sync(frm, "all", "", "price");
	await settle();
	item.start_sync(frm, "all", "", "stock");
	await settle();

	const by = (suffix) => calls.filter((c) => c.method.endsWith(suffix)).map((c) => c.args);
	const expected = [
		{ account: "NOON-A", mapping_names: ["N-1", "N-2"] },
		{ account: "NOON-B", mapping_names: ["N-3"] },
	];
	assert.deepEqual(by("noon_catalog.queue_account_sync"), expected);
	assert.deepEqual(by("noon_pricing.queue_pricing_push"), expected);
	assert.deepEqual(by("noon_inventory.queue_stock_push"), expected);
});

test("Talabat sends price and stock together through the mappings sync and says it confirms later", async () => {
	const alerts = [];
	const { frm, calls } = await loaded_frm(
		[make_row({ platform: "Talabat" })],
		{
			"marketplace_integrations.api.talabat_catalog.queue_mappings_catalog_sync": () =>
				Promise.resolve({ message: { queued: 1, jobs: 1 } }),
		},
		{ msgprint: (args) => alerts.push(args) }
	);

	item.start_sync(frm, "row", "MPM-1", "both");
	await settle();

	const call = calls.find((c) => c.method.endsWith("talabat_catalog.queue_mappings_catalog_sync"));
	assert.deepEqual(call.args, { mapping_names: ["MPM-1"] });
	assert.match(alerts[0].message, /Talabat confirms later\. Refresh to see the result\./);
});

test("a failed Talabat call does not add the confirms-later line", async () => {
	const alerts = [];
	const { frm } = await loaded_frm(
		[make_row({ platform: "Talabat" })],
		{
			"marketplace_integrations.api.talabat_catalog.queue_mappings_catalog_sync": () => Promise.reject(new Error("boom")),
		},
		{ msgprint: (args) => alerts.push(args) }
	);

	item.start_sync(frm, "row", "MPM-1", "both");
	await settle();

	assert.match(alerts[0].message, /boom/);
	assert.doesNotMatch(alerts[0].message, /confirms later/);
});

test("the automatic-push warning follows the kind being sent", () => {
	install_frappe();
	const price_only_off = [{ row: make_row({ price_push_off: true, account_name: "Noon A" }) }];
	const stock_only_off = [{ row: make_row({ stock_push_off: true, account_name: "Noon A" }) }];
	const warning = /automatic push is off for Noon A/;

	assert.match(item.confirm_message("row", price_only_off, "price"), warning);
	assert.doesNotMatch(item.confirm_message("row", price_only_off, "stock"), /automatic push/);
	assert.match(item.confirm_message("row", price_only_off, "both"), warning);
	assert.match(item.confirm_message("row", stock_only_off, "stock"), warning);
	assert.doesNotMatch(item.confirm_message("row", stock_only_off, "price"), /automatic push/);
});

test("Talabat Price only never calls an endpoint and explains why", async () => {
	const alerts = [];
	const { frm, calls } = await loaded_frm([make_row({ platform: "Talabat" })], {}, { msgprint: (args) => alerts.push(args) });

	item.start_sync(frm, "row", "MPM-1", "price");
	await settle();

	assert.equal(calls.length, 1, "only the listings fetch");
	assert.match(alerts[0].message, /Talabat updates price and stock together/);
});

test("the confirm names the automatic-push-off accounts and the Talabat inactive / stock 0 consequence", async () => {
	install_frappe();
	const rows = [
		{ row: make_row({ stock_push_off: true, account_name: "Shop <A>" }) },
		{ row: make_row({ name: "T", platform: "Talabat", enabled: 0, account_name: "Tal" }), talabat_inactive: true },
	];

	const message = item.confirm_message("all", rows, "both");

	assert.match(message, /Send the current price and stock of 2 listings of this Item to their marketplaces\?/);
	assert.match(message, /This runs once even though automatic push is off for Shop &lt;A&gt;\./);
	assert.match(message, /This sends the listing to Talabat as inactive with stock 0\./);

	const plain = item.confirm_message("row", [{ row: make_row() }], "both");
	assert.doesNotMatch(plain, /automatic push/);
});

test("Sync all listings sends only the writable rows and names the ones it left out", async () => {
	const alerts = [];
	const rows = [
		make_row({ name: "S-1", external_reference: "OK-1" }),
		make_row({ name: "N-1", platform: "Noon", marketplace_account: "NOON-A", external_reference: "NOON-REF", can_write: false }),
		make_row({ name: "S-2", external_reference: "OFF-1", enabled: 0 }),
	];
	const { frm, calls } = await loaded_frm(
		rows,
		{
			"marketplace_integrations.api.shopify_catalog_push.queue_catalog_push": () => Promise.resolve({ message: { jobs: [] } }),
			"marketplace_integrations.api.shopify_inventory.queue_inventory_sync": () => Promise.resolve({ message: { jobs: [] } }),
		},
		{ msgprint: (args) => alerts.push(args) }
	);

	item.start_sync(frm, "all", "", "both");
	await settle();

	const price = calls.find((c) => c.method.endsWith("queue_catalog_push"));
	assert.deepEqual(price.args, { mapping_names: ["S-1"] });
	assert.equal(calls.some((c) => c.method.includes("noon")), false);
	const message = alerts[0].message;
	assert.match(message, /Skipped/);
	assert.match(message, /NOON-REF: You do not have permission to sync this listing\./);
	assert.match(message, /OFF-1: This listing is disabled\./);
});

test("a double click queues once and the clicked control shows Queuing...", async () => {
	let release;
	const gate = new Promise((resolve) => {
		release = resolve;
	});
	const { frm, calls } = await loaded_frm(
		[make_row({ platform: "Noon" })],
		{
			"marketplace_integrations.api.noon_catalog.queue_account_sync": () =>
				gate.then(() => ({ message: { pricing: { queued: true }, stock: { queued: true } } })),
		}
	);

	item.start_sync(frm, "row", "MPM-1", "both");
	item.start_sync(frm, "row", "MPM-1", "both");
	await settle();

	assert.equal(calls.filter((c) => c.method.endsWith("queue_account_sync")).length, 1);
	assert.match(frm.wrapper.last, /Queuing\.\.\./);
	assert.match(frm.wrapper.last, /data-kind="price"[^>]*disabled/);

	release();
	await settle();
	assert.doesNotMatch(frm.wrapper.last, /Queuing\.\.\./);
	// The control works again afterwards.
	item.start_sync(frm, "row", "MPM-1", "both");
	await settle();
	assert.equal(calls.filter((c) => c.method.endsWith("queue_account_sync")).length, 2);
});

test("answering No to the confirm queues nothing and frees the control", async () => {
	const { frm, calls } = await loaded_frm([make_row()], {}, { confirm: (message, yes, no) => no() });

	item.start_sync(frm, "row", "MPM-1", "both");
	item.start_sync(frm, "row", "MPM-1", "both");

	assert.equal(calls.length, 1);
	assert.equal(item.get_state(frm).busy, false);
});

test("a failed leg is reported as Failed with the escaped server message and the tab still re-fetches", async () => {
	const alerts = [];
	const { frm, calls } = await loaded_frm(
		[make_row({ platform: "Noon" })],
		{
			"marketplace_integrations.api.noon_catalog.queue_account_sync": () =>
				Promise.reject({ responseJSON: { _server_messages: JSON.stringify([JSON.stringify({ message: "No <script>x</script> price list" })]) } }),
		},
		{ msgprint: (args) => alerts.push(args) }
	);
	const before = calls.filter((c) => c.method === LISTINGS).length;

	item.start_sync(frm, "row", "MPM-1", "both");
	await settle();

	assert.equal(alerts[0].indicator, "red");
	assert.match(alerts[0].message, /No x price list/);
	assert.doesNotMatch(alerts[0].message, /<script>/);
	assert.equal(calls.filter((c) => c.method === LISTINGS).length, before + 1);
});
