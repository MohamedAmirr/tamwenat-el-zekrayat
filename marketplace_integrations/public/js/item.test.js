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

test("each listing is a card with labelled fields, references in a ltr bdi", async () => {
	const { frm, calls } = await loaded_frm([
		make_row(),
		make_row({ name: "MPM-2", platform: "Talabat", account_name: "Talabat A", external_reference: "T-9" }),
	]);

	assert.equal(frm.toggled.custom_marketplace_listings_tab, true);
	assert.equal(calls[0].type, "GET");
	assert.deepEqual(calls[0].args, { item: "ITEM-1" });
	const html = frm.wrapper.last;
	for (const label of ["External Reference", "Sales UOM", "Price", "Stock"]) {
		assert.ok(html.includes(`>${label}<`), `${label} label renders`);
	}
	assert.match(html, /<bdi dir="ltr">REF-1<\/bdi>/);
	assert.match(html, /badge[^>]*>Shopify</);
	assert.match(html, /mi-listing-card/);
	assert.doesNotMatch(html, /grid-heading-row|grid-row/);
	assert.match(html, /indicator-pill green/);
});

function card_from(html, name) {
	const start = html.indexOf(`data-name="${name}"`);
	return html.slice(start);
}

test("the account name, platform badge and enabled status share the card header", async () => {
	const { frm } = await loaded_frm([make_row()]);
	const html = frm.wrapper.last;

	assert.doesNotMatch(html, />Platform</);
	assert.match(
		html,
		/<strong class="ellipsis"[^>]*title="Shop A">Shop A<\/strong><span class="badge badge-light"[^>]*>Shopify<\/span><span class="indicator-pill green[^>]*><span>Enabled</
	);
});

test("price and stock status are two labelled groups, each with its own pill", async () => {
	const { frm } = await loaded_frm([make_row({ sync_status: "Error", inventory_sync_status: "Queued" })]);
	const body = card_from(frm.wrapper.last, "MPM-1");

	const price = body.match(/<div role="group" aria-label="Price"[^>]*>(.*?)<\/div>/);
	const stock = body.match(/<div role="group" aria-label="Stock"[^>]*>(.*?)<\/div>/);
	assert.ok(price && stock, "both groups render");
	assert.match(price[1], /indicator-pill red[^>]*><span>Error</);
	assert.match(stock[1], /indicator-pill blue[^>]*><span>Queued</);
});

test("a Talabat card has one Price and stock status and no Stock group", async () => {
	const { frm } = await loaded_frm([make_row({ platform: "Talabat", inventory_sync_status: "Error" })]);
	const body = card_from(frm.wrapper.last, "MPM-1");

	assert.match(body, /aria-label="Price and stock"/);
	assert.doesNotMatch(body, /aria-label="Stock"/);
	assert.doesNotMatch(body, /indicator-pill red/);
});

test("an unset Sales UOM is left out of the card", async () => {
	const { frm } = await loaded_frm([make_row({ sales_uom: "" })]);

	assert.doesNotMatch(frm.wrapper.last, />Sales UOM</);
});

test("the sync control sits inside the card and stays on one line", async () => {
	const { frm } = await loaded_frm([make_row()]);
	const html = frm.wrapper.last;

	assert.match(html, /<div class="btn-group mi-sync" role="group" style="white-space: nowrap">/);
	assert.match(html, /<button type="button" class="btn btn-default btn-xs" style="white-space: nowrap"[^>]*>Sync price and stock</);
	const card = html.slice(html.indexOf('class="mi-listing-card"'));
	assert.ok(card.indexOf("mi-sync-cell") > 0, "the per-listing control is inside the card");
});

test("only logical CSS properties are used for layout", async () => {
	const { frm } = await loaded_frm([make_row()]);

	assert.doesNotMatch(frm.wrapper.last, /(?:margin|padding)-(?:left|right)|(?:^|[\s;"])(?:left|right):|text-align: *(?:left|right)/);
});

test("a long external reference wraps inside the card and keeps its tooltip and ltr bdi", async () => {
	const long_ref = "gid://shopify/ProductVariant/" + "9".repeat(80);
	const { frm } = await loaded_frm([make_row({ external_reference: long_ref })]);
	const html = frm.wrapper.last;

	assert.match(html, /overflow-wrap: anywhere" title="gid:\/\/shopify\/ProductVariant\/9+"><bdi dir="ltr">gid:/);
});

test("a Talabat card ignores the stored inventory status", async () => {
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

// ---- Create on Shopify (#495) ----------------------------------------------------------------

const QUEUE_CREATE = "marketplace_integrations.api.shopify_products.queue_product_create";
const CREATE_STATUS = "marketplace_integrations.api.shopify_products.get_product_create_status";
const GET_LIST = "marketplace_integrations.api.shopify_products.list_product_create_accounts";

function install_dialog() {
	const made = [];
	class Dialog {
		constructor(args) {
			this.args = args;
			this.fields = {};
			this.values = { account: args.fields[0].default };
			this.hidden = false;
			this.disabled = false;
			made.push(this);
		}
		show() {}
		hide() {
			this.hidden = true;
		}
		get_field(name) {
			if (!this.fields[name]) {
				const field = { last: "" };
				field.$wrapper = { html: (markup) => (field.last = markup) };
				this.fields[name] = field;
			}
			return this.fields[name];
		}
		get_value(name) {
			return this.values[name];
		}
		disable_primary_action() {
			this.disabled = true;
		}
		enable_primary_action() {
			this.disabled = false;
		}
		set_primary_action(label, fn) {
			this.retry = { label, fn };
		}
	}
	global.frappe.ui.Dialog = Dialog;
	return made;
}

async function create_frm(accounts, extra = {}, frappe_overrides = {}) {
	const { frm, calls } = await loaded_frm(
		[],
		{ [GET_LIST]: () => Promise.resolve({ message: accounts }), ...extra },
		{ show_alert() {}, ...frappe_overrides }
	);
	frm.doc.item_name = "Nice <b>Item</b>";
	return { frm, calls, dialogs: install_dialog() };
}

test("Create on Shopify shows for a saved, enabled, non-template Item the user can map", async () => {
	const { frm } = await loaded_frm([]);
	assert.match(frm.wrapper.last, /data-action="create_shopify"/);
});

test("Create on Shopify is hidden for a disabled or template Item and without mapping create", async () => {
	for (const doc of [{ disabled: 1 }, { has_variants: 1 }]) {
		const { frm } = await loaded_frm([]);
		Object.assign(frm.doc, doc);
		await item.render_listings_tab(frm);
		assert.doesNotMatch(frm.wrapper.last, /create_shopify/);
	}
	const { frm } = await loaded_frm([], {}, { model: { can_read: () => true, can_write: () => true, can_create: () => false } });
	assert.doesNotMatch(frm.wrapper.last, /create_shopify/);
});

test("a single enabled Shopify account is preselected and the preview is escaped", async () => {
	const { frm, dialogs } = await create_frm([{ name: "MKT-1", account_name: "Shop A" }]);

	await item.open_create_on_shopify(frm);

	assert.equal(dialogs.length, 1);
	assert.equal(dialogs[0].args.fields[0].default, "MKT-1");
	const preview = dialogs[0].args.fields[1].options;
	assert.match(preview, /Nice &lt;b&gt;Item&lt;\/b&gt;/);
	assert.match(preview, /ITEM-1/);
	assert.match(preview, /Draft/);
});

test("the account list comes from the write-filtered server method, not a read list", async () => {
	const { frm, calls } = await create_frm([{ name: "MKT-1" }]);
	await item.open_create_on_shopify(frm);
	assert.ok(calls.some((c) => c.method === GET_LIST));
	assert.ok(!calls.some((c) => c.method === "frappe.client.get_list"));
});

test("a single account has no blank option", async () => {
	const { frm, dialogs } = await create_frm([{ name: "MKT-1", account_name: "Shop A" }]);
	await item.open_create_on_shopify(frm);
	assert.deepEqual(dialogs[0].args.fields[0].options, [{ value: "MKT-1", label: "Shop A" }]);
});

test("several accounts start with a blank option and nothing preselected", async () => {
	const { frm, dialogs } = await create_frm([{ name: "A", account_name: "Shop A" }, { name: "B" }]);
	await item.open_create_on_shopify(frm);
	const field = dialogs[0].args.fields[0];
	assert.deepEqual(field.options, [
		{ value: "", label: "" },
		{ value: "A", label: "Shop A" },
		{ value: "B", label: "B" },
	]);
	assert.equal(field.default, "");
	assert.equal(field.reqd, 1);
});

test("a failed job whose product is already mapped offers Close, not Retry, and reloads the listings", async () => {
	const { frm, calls, dialogs } = await create_frm([{ name: "MKT-1" }], {
		[QUEUE_CREATE]: () => Promise.resolve({ message: { job_id: "j1" } }),
		[CREATE_STATUS]: () =>
			Promise.resolve({ message: { status: "failed", mapped: true, error: "Mapped, fix the SKU in Shopify" } }),
	});
	await item.open_create_on_shopify(frm);
	const before = calls.filter((c) => c.method === LISTINGS).length;

	dialogs[0].args.primary_action({ account: "MKT-1" });
	await settle();

	assert.match(dialogs[0].fields.status_html.last, /fix the SKU in Shopify/);
	assert.equal(dialogs[0].retry.label, "Close");
	assert.equal(calls.filter((c) => c.method === LISTINGS).length, before + 1);
	dialogs[0].retry.fn();
	assert.equal(dialogs[0].hidden, true);
});

test("success hides the dialog and reloads the listings", async () => {
	const alerts = [];
	const { frm, calls, dialogs } = await create_frm(
		[{ name: "MKT-1" }],
		{
			[QUEUE_CREATE]: () => Promise.resolve({ message: { job_id: "j1" } }),
			[CREATE_STATUS]: () => Promise.resolve({ message: { status: "completed" } }),
		},
		{ show_alert: (args) => alerts.push(args) }
	);
	await item.open_create_on_shopify(frm);
	const before = calls.filter((c) => c.method === LISTINGS).length;

	dialogs[0].args.primary_action({ account: "MKT-1" });
	await settle();

	assert.deepEqual(calls.find((c) => c.method === QUEUE_CREATE).args, { account: "MKT-1", item: "ITEM-1" });
	assert.equal(dialogs[0].hidden, true);
	assert.equal(alerts[0].indicator, "green");
	assert.equal(calls.filter((c) => c.method === LISTINGS).length, before + 1);
});

async function create_with_result(result) {
	const alerts = [];
	const { frm, calls, dialogs } = await create_frm(
		[{ name: "MKT-1" }],
		{
			[QUEUE_CREATE]: () => Promise.resolve({ message: { job_id: "j1" } }),
			[CREATE_STATUS]: () => Promise.resolve({ message: { status: "completed", result } }),
		},
		{ show_alert: (args, seconds, actions) => alerts.push({ ...args, actions }) }
	);
	await item.open_create_on_shopify(frm);
	dialogs[0].args.primary_action({ account: "MKT-1" });
	await settle();
	return { alerts, calls };
}

test("when the price was sent the success alert is the plain green one", async () => {
	const { alerts } = await create_with_result({ mapping: "MPM-9", price_sent: true });

	assert.equal(alerts[0].indicator, "green");
	assert.equal(alerts[0].message, "Created on Shopify as a draft product.");
});

test("a skipped price is named in the alert, escaped, without Retry for an expected reason", async () => {
	const { alerts } = await create_with_result({
		mapping: "MPM-9",
		price_sent: false,
		price_skipped_reason: "No <b>Item Price</b>",
		price_retry: false,
	});

	assert.equal(alerts[0].indicator, "orange");
	assert.match(alerts[0].message, /The price was not sent: No &lt;b&gt;Item Price&lt;\/b&gt;/);
	assert.deepEqual(Object.keys(alerts[0].actions), []);
});

test("an unexpected price error offers Retry, which reloads the listings for the price-only sync", async () => {
	const { alerts, calls } = await create_with_result({
		mapping: "MPM-9",
		price_sent: false,
		price_skipped_reason: "An unexpected error occurred while looking up the price.",
		price_retry: true,
	});
	assert.deepEqual(Object.keys(alerts[0].actions), ["Retry"]);
	const before = calls.filter((c) => c.method === LISTINGS).length;

	alerts[0].actions.Retry();
	await settle();

	assert.equal(calls.filter((c) => c.method === LISTINGS).length, before + 1);
});

test("a server refusal is shown escaped and Retry queues again", async () => {
	let queued = 0;
	const { frm, dialogs } = await create_frm([{ name: "MKT-1" }], {
		[QUEUE_CREATE]: () => {
			queued += 1;
			return Promise.reject({
				responseJSON: { _server_messages: JSON.stringify([JSON.stringify({ message: "Already <script>x</script> mapped" })]) },
			});
		},
	});
	await item.open_create_on_shopify(frm);

	dialogs[0].args.primary_action({ account: "MKT-1" });
	await settle();

	assert.match(dialogs[0].fields.status_html.last, /Already x mapped/);
	assert.doesNotMatch(dialogs[0].fields.status_html.last, /<script>/);
	assert.equal(dialogs[0].retry.label, "Retry");
	assert.equal(dialogs[0].disabled, false);
	dialogs[0].retry.fn();
	await settle();
	assert.equal(queued, 2);
});

test("a failed job shows its error escaped in the dialog", async () => {
	const { frm, dialogs } = await create_frm([{ name: "MKT-1" }], {
		[QUEUE_CREATE]: () => Promise.resolve({ message: { job_id: "j1" } }),
		[CREATE_STATUS]: () => Promise.resolve({ message: { status: "failed", error: "Bad <i>title</i>" } }),
	});
	await item.open_create_on_shopify(frm);

	dialogs[0].args.primary_action({ account: "MKT-1" });
	await settle();

	assert.match(dialogs[0].fields.status_html.last, /Bad &lt;i&gt;title&lt;\/i&gt;/);
	assert.equal(dialogs[0].hidden, false);
});
