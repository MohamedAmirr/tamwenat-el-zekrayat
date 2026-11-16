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
		// Whole-day difference of YYYY-MM-DD strings, like frappe.datetime.get_diff; "today" is fixed.
		datetime: {
			get_today: () => "2026-11-04",
			get_diff: (a, b) => Math.round((Date.parse(`${a}T00:00:00Z`) - Date.parse(`${b}T00:00:00Z`)) / 86400000),
			str_to_user: (value) => `user:${value}`,
		},
		// Frappe 16.52's badge: orange is amber, yellow renders like orange, gray is the default theme.
		ui: {
			badge: {
				html: ({ label, theme, title }) =>
					`<span class="es-badge" data-theme="${theme}" title="${global.frappe.utils.escape_html(title)}">` +
					`<span class="es-badge__label">${global.frappe.utils.escape_html(label)}</span></span>`,
			},
		},
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

test("the list loads the platform and stage fields without dropping ERPNext's own fields", () => {
	const { settings } = load();
	assert.ok(settings.add_fields.includes("custom_marketplace_platform"));
	global.frappe.listview_settings["Sales Order"].add_fields = ["status"];
	const path = require.resolve("./sales_order_list.js");
	delete require.cache[path];
	require(path);
	assert.deepEqual(global.frappe.listview_settings["Sales Order"].add_fields, [
		"status",
		"custom_marketplace_platform",
		"custom_earliest_delivery_date",
		"custom_order_stage",
		"custom_needs_attention",
		"custom_attention_reason",
		"advance_payment_status",
	]);
});

test("a flagged order shows the red Needs Attention pill before Waiting for Stock, with its own reason", () => {
	const { module_exports } = load();
	const reason = 'Shopify changed the order. Next step: open it <b>now</b>.';
	const info = module_exports.status_info(
		row("SO-1", { custom_needs_attention: 1, custom_attention_reason: reason, custom_order_stage: "Waiting for Stock" })
	);
	assert.equal(info.label, "Needs Attention");
	assert.equal(info.color, "red");
	assert.equal(info.tip, reason);
	assert.equal(info.filter, "custom_needs_attention,=,1");
	const html = global.frappe.ui.badge.html({ label: info.label, theme: info.color, title: info.tip });
	assert.ok(!html.includes("<b>"));
});

test("an unflagged order is not touched by Needs Attention, and the legend lists it", () => {
	const { module_exports } = load();
	assert.equal(module_exports.status_info(row("SO-1", { custom_needs_attention: 0 })), null);
	// a cancelled order keeps ERPNext's own indicator
	assert.equal(module_exports.status_info(row("SO-1", { docstatus: 2, custom_needs_attention: 1, custom_attention_reason: "x" })), null);
	// Closed and On Hold submitted orders still show the pill
	assert.equal(module_exports.status_info(row("SO-1", { status: "Closed", custom_needs_attention: 1 })).label, "Needs Attention");
	assert.match(module_exports.legend_html("status"), /Needs Attention/);
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

test("other formatters of ERPNext's list are kept", () => {
	const existing = () => "existing";
	const { settings } = load({ original_formatters: { another_field: existing } });
	assert.equal(settings.formatters.another_field, existing);
	assert.ok(settings.formatters.custom_payment_display);
});

// -- platform column -------------------------------------------------------------------------

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

// -- Status and Payment columns --------------------------------------------------------------

const marketplace_order = (extra = {}) => ({
	docstatus: 1,
	status: "To Deliver and Bill",
	custom_marketplace_platform: "Noon",
	...extra,
});

// A list row: a submitted order with a name and no doctype key (a form document has one).
const row = (name, extra = {}) => ({ name, docstatus: 1, status: "To Deliver", ...extra });

// Answers the page call and re-renders, like the server does for the rows of one page.
async function load_page(module_exports, answer, rows) {
	module_exports.reset_states();
	const calls = [];
	global.frappe.call = (options) => {
		calls.push(options);
		return typeof answer === "function" ? answer(options) : Promise.resolve({ message: answer });
	};
	const view = { data: rows, renders: 0, render() { this.renders++; } };
	await module_exports.load_states(view);
	return { calls, view };
}

test("one call per page asks for the submitted orders only and re-renders", async () => {
	const { module_exports } = load();
	const { calls, view } = await load_page(
		module_exports,
		{ "SO-1": { stage: "ready", payment: "cod" } },
		[row("SO-1"), row("SO-2", { docstatus: 0 }), row("SO-3", { docstatus: 2 })]
	);
	assert.equal(calls.length, 1);
	assert.equal(calls[0].method, "marketplace_integrations.api.order_list_status.get_order_states");
	assert.equal(calls[0].args.names, '["SO-1"]');
	assert.equal(calls[0].silent, true);
	assert.equal(view.renders, 1);
});

test("a page with no submitted order makes no call", async () => {
	const { module_exports } = load();
	const { calls } = await load_page(module_exports, {}, [row("SO-2", { docstatus: 0 })]);
	assert.equal(calls.length, 0);
});

test("each stage and payment shows one plain pill with a colour and a hover line", async () => {
	const { module_exports } = load();
	const cases = [
		[{ stage: "not_prepared", payment: "cod" }, "Not prepared yet", "gray"],
		[{ stage: "waiting_for_goods", payment: "cod" }, "Not prepared yet", "gray"],
		[{ stage: "ready", payment: "cod" }, "Ready, waiting for pickup", "blue"],
		[{ stage: "ready", payment: "advance" }, "Ready, waiting for pickup", "blue"],
		[{ stage: "on_the_way", payment: "invoice" }, "On the way", "orange"],
		[{ stage: "delivered", payment: "cod" }, "Delivered, payment not received", "violet"],
		[{ stage: "delivered", payment: "partially_paid" }, "Delivered, payment not received", "violet"],
		[{ stage: "delivered", payment: "advance" }, "Delivered and paid", "green"],
		[{ stage: "delivered", payment: "invoice" }, "Delivered and paid", "green"],
		[{ stage: "delivered", payment: "marketplace" }, "Delivered and paid", "green"],
		[{ stage: "problem", payment: "cod" }, "Delivery problem", "red"],
		[{ stage: "attempt_failed", payment: "cod" }, "Delivery problem", "red"],
	];
	for (const [state, label, color] of cases) {
		await load_page(module_exports, { "SO-1": state }, [row("SO-1")]);
		const info = module_exports.status_info(row("SO-1"));
		assert.equal(info.label, label);
		assert.equal(info.color, color);
		assert.ok(info.tip.length > 10 && info.tip.endsWith("."), info.tip);
		// every pill filters the list like ERPNext's own pill does, never an empty data-filter
		assert.equal(info.filter, "status,=,To Deliver");
	}
});

test("a partly paid order shows the Partially paid pill and the amount still owed on hover", async () => {
	const { module_exports } = load();
	await load_page(module_exports, { "SO-1": { stage: "ready", payment: "partially_paid", owed: 25 } }, [row("SO-1", { currency: "EGP" })]);
	const html = module_exports.format_payment("", {}, row("SO-1", { currency: "EGP" }));
	assert.match(html, /Partially paid/);
	assert.match(html, /Still owed: 25\.00 EGP/);
});

test("the colours on screen differ: on the way is not the same badge colour as delivered, payment not received", async () => {
	const { module_exports } = load();
	await load_page(module_exports, { "SO-1": { stage: "on_the_way", payment: "cod" }, "SO-2": { stage: "delivered", payment: "cod" } }, [row("SO-1"), row("SO-2")]);
	// Frappe's badge draws orange and yellow the same (amber), so the two must not both use them
	const theme = (name) => module_exports.status_info(row(name)).color.replace("orange", "amber").replace("yellow", "amber");
	assert.notEqual(theme("SO-1"), theme("SO-2"));
});

test("a failed delivery attempt and a returned parcel are both red 'Delivery problem' with their own tips", async () => {
	const { module_exports } = load();
	await load_page(module_exports, { "SO-1": { stage: "attempt_failed", payment: "cod" }, "SO-2": { stage: "problem", payment: "cod" } }, [row("SO-1"), row("SO-2")]);
	assert.equal(module_exports.status_info(row("SO-1")).tip, "A delivery attempt failed. Next step: follow up with Bosta.");
	assert.match(module_exports.status_info(row("SO-2")).tip, /came back or was lost/);
	assert.equal(module_exports.status_info(row("SO-1")).label, module_exports.status_info(row("SO-2")).label);
});

test("a Delivery Note waiting for goods does not get the create-the-note tip", async () => {
	const { module_exports } = load();
	await load_page(module_exports, { "SO-1": { stage: "waiting_for_goods", payment: "cod" }, "SO-2": { stage: "not_prepared", payment: "cod" } }, [row("SO-1"), row("SO-2")]);
	assert.match(module_exports.status_info(row("SO-1")).tip, /waiting for goods/);
	assert.doesNotMatch(module_exports.status_info(row("SO-1")).tip, /create the Delivery Note/);
	assert.match(module_exports.status_info(row("SO-2")).tip, /create the Delivery Note/);
});

test("the Status indicator replaces ERPNext's and its colour is never the only signal", async () => {
	const { module_exports, settings } = load();
	await load_page(module_exports, { "SO-1": { stage: "on_the_way", payment: "cod" } }, [row("SO-1")]);
	assert.deepEqual(settings.get_indicator(row("SO-1")).slice(0, 2), ["On the way", "orange"]);
});

test("a row of a page that is about to be asked for shows Loading... until the page answers", () => {
	const { module_exports } = load();
	module_exports.reset_states();
	module_exports.mark_expected({ data: [row("SO-9")] });
	assert.equal(module_exports.status_info(row("SO-9")).label, "Loading...");
	assert.equal(module_exports.status_info(row("SO-9")).filter, "status,=,To Deliver");
	assert.match(module_exports.format_payment("", {}, row("SO-9")), />Loading\.\.\.</);
});

test("a row nobody asked about (Quick List, other views) keeps ERPNext's indicator instead of Loading... for ever", () => {
	const { module_exports, settings } = load();
	module_exports.reset_states();
	assert.equal(module_exports.status_info(row("SO-9")), null);
	assert.equal(module_exports.format_payment("", {}, row("SO-9")), "");
	assert.equal(settings.get_indicator(row("SO-9")), undefined);
});

test("before_render marks the rows of the open list as expected", () => {
	const { settings, module_exports } = load();
	module_exports.reset_states();
	const view = { data: [row("SO-1"), row("SO-2", { docstatus: 0 })] };
	settings.onload(view);
	settings.before_render();
	assert.equal(module_exports.status_info(row("SO-1")).label, "Loading...");
	assert.equal(module_exports.status_info(row("SO-2", { docstatus: 0, status: "Draft" })), null);
});

test("an older answer that arrives after a newer one does not overwrite it", async () => {
	const { module_exports } = load();
	module_exports.reset_states();
	const resolvers = [];
	global.frappe.call = () => new Promise((resolve) => resolvers.push(resolve));
	const view = { data: [row("SO-1")], render() {} };
	const first = module_exports.load_states(view);
	const second = module_exports.load_states(view);
	resolvers[1]({ message: { "SO-1": { stage: "delivered", payment: "advance" } } });
	await second;
	resolvers[0]({ message: { "SO-1": { stage: "ready", payment: "cod" } } });
	await first;
	assert.equal(module_exports.status_info(row("SO-1")).label, "Delivered and paid");
	assert.match(module_exports.format_payment("", {}, row("SO-1")), /Paid in advance/);
});

test("a failed older request does not hide a newer answer, and a failed newer request does hide an older one", async () => {
	const { module_exports } = load();
	module_exports.reset_states();
	const original_error = console.error;
	console.error = () => {};
	try {
		const calls = [];
		global.frappe.call = () => new Promise((resolve, reject) => calls.push({ resolve, reject }));
		const view = { data: [row("SO-1")], render() {} };
		const first = module_exports.load_states(view);
		const second = module_exports.load_states(view);
		calls[1].resolve({ message: { "SO-1": { stage: "ready", payment: "cod" } } });
		await second;
		calls[0].reject(new Error("offline"));
		await first;
		assert.equal(module_exports.status_info(row("SO-1")).label, "Ready, waiting for pickup");
		// now the newest request fails: the older answer is no longer shown as current
		global.frappe.call = () => Promise.reject(new Error("offline"));
		await module_exports.load_states(view);
		assert.equal(module_exports.status_info(row("SO-1")), null);
		assert.equal(module_exports.format_payment("", {}, row("SO-1")), "");
	} finally {
		console.error = original_error;
	}
});

test("ERPNext's own indicator stays for Draft, Cancelled, Closed, On Hold and a pending advance", async () => {
	const { module_exports } = load();
	await load_page(module_exports, { "SO-1": { stage: "ready", payment: "cod" } }, [row("SO-1")]);
	for (const extra of [
		{ docstatus: 0, status: "Draft" },
		{ docstatus: 2, status: "Cancelled" },
		{ status: "Closed" },
		{ status: "On Hold" },
		{ advance_payment_status: "Requested" },
	]) {
		assert.equal(module_exports.status_info(row("SO-1", extra)), null, JSON.stringify(extra));
	}
});

test("a Completed order is covered by the new Status", async () => {
	const { module_exports } = load();
	await load_page(module_exports, { "SO-1": { stage: "delivered", payment: "invoice" } }, [row("SO-1", { status: "Completed" })]);
	assert.equal(module_exports.status_info(row("SO-1", { status: "Completed" })).label, "Delivered and paid");
});

test("an order in the stage Waiting for Stock shows a red indicator before any delivery stage (#500)", async () => {
	const { module_exports, settings } = load();
	assert.ok(settings.add_fields.includes("custom_order_stage"));
	await load_page(module_exports, { "SO-1": { stage: "ready", payment: "cod" } }, [row("SO-1")]);
	const waiting = row("SO-1", { custom_order_stage: "Waiting for Stock" });
	assert.deepEqual(settings.get_indicator(waiting).slice(0, 2), ["Waiting for Stock", "red"]);
	assert.equal(settings.get_indicator(waiting)[2], "custom_order_stage,=,Waiting for Stock");
	// not for a Closed order (ERPNext's own indicator stays)
	assert.equal(settings.get_indicator({ ...waiting, status: "Closed" }), undefined);
	assert.equal(settings.get_indicator(row("SO-1", { custom_order_stage: "Confirmed" })).slice(0, 2)[0], "Ready, waiting for pickup");
});

test("the form header keeps ERPNext's indicator: a full document has a doctype, a list row does not", async () => {
	const { module_exports } = load();
	await load_page(module_exports, { "SO-1": { stage: "ready", payment: "cod" } }, [row("SO-1")]);
	assert.equal(module_exports.status_info({ ...row("SO-1"), doctype: "Sales Order" }), null);
});

test("when the page call fails the native indicator stays, Payment is empty and the list still renders", async () => {
	const { module_exports } = load();
	const original_error = console.error;
	console.error = () => {};
	let view;
	try {
		({ view } = await load_page(module_exports, () => Promise.reject(new Error("offline")), [row("SO-1")]));
	} finally {
		console.error = original_error;
	}
	assert.equal(module_exports.status_info(row("SO-1")), null);
	assert.equal(module_exports.format_payment("", {}, row("SO-1")), "");
	assert.equal(view.renders, 1);
	// the next page that answers recovers
	await load_page(module_exports, { "SO-1": { stage: "ready", payment: "cod" } }, [row("SO-1")]);
	assert.equal(module_exports.status_info(row("SO-1")).label, "Ready, waiting for pickup");
});

test("the Payment pill: paid in advance and by invoice are green, pay on delivery is grey, each with a hover line", async () => {
	const { module_exports, settings } = load();
	const format = settings.formatters.custom_payment_display;
	const answer = {
		"SO-1": { stage: "ready", payment: "advance" },
		"SO-2": { stage: "ready", payment: "invoice" },
		"SO-3": { stage: "ready", payment: "cod" },
		"SO-4": { stage: "ready", payment: "marketplace" },
	};
	await load_page(module_exports, answer, [row("SO-1"), row("SO-2"), row("SO-3"), row("SO-4")]);
	assert.match(format("", {}, row("SO-4")), /data-theme="green" title="[^"]+">.*Paid by marketplace</);
	const html = (name) => format("", {}, row(name));
	// the same Espresso badge as the Status column, with the meaning as its title
	assert.match(html("SO-1"), /^<span class="es-badge" data-theme="green" title="[^"]+">.*Paid in advance.*<\/span>$/);
	assert.match(html("SO-2"), /data-theme="green" title="[^"]+">.*Paid \(invoice\)</);
	assert.match(html("SO-3"), /data-theme="gray" title="[^"]+">.*Pay on delivery</);
	// drafts, cancelled orders and a missing doc show nothing
	assert.equal(format("", {}, row("SO-1", { docstatus: 0 })), "");
	assert.equal(format("", {}, row("SO-1", { docstatus: 2 })), "");
	assert.equal(format(""), "");
});

test("a state the server does not know shows no Payment pill and the native Status", async () => {
	const { module_exports } = load();
	await load_page(module_exports, { "SO-1": { stage: "odd", payment: "odd" } }, [row("SO-1")]);
	assert.equal(module_exports.format_payment("", {}, row("SO-1")), "");
	assert.equal(module_exports.status_info(row("SO-1")), null);
});

test("the legend lists every state once with its meaning, escaped", () => {
	const { module_exports } = load();
	const status = module_exports.legend_html("status");
	// "Not prepared yet" appears twice: nothing made yet, and a note waiting for goods (a different next step)
	assert.equal(status.split(">Not prepared yet<").length - 1, 2);
	// "Delivery problem" too: a failed attempt (follow up with Bosta) and a returned or lost parcel
	assert.equal(status.split(">Delivery problem<").length - 1, 2);
	assert.match(status, /A delivery attempt failed/);
	for (const label of [
		"Ready, waiting for pickup",
		"On the way",
		"Delivered, payment not received",
		"Delivered and paid",
		"Waiting for Stock",
	]) {
		assert.equal(status.split(`>${label}<`).length - 1, 1, label);
	}
	assert.match(status, /least advanced/);
	const payment = module_exports.legend_html("payment");
	for (const label of ["Paid in advance", "Paid (invoice)", "Paid by marketplace", "Pay on delivery"]) assert.ok(payment.includes(`>${label}<`), label);
	assert.ok(!payment.includes("least advanced"));
});

test("decorate puts the meaning in the Status pill title and adds one ? button per column header", () => {
	const { module_exports } = load();
	module_exports.reset_states();
	module_exports.mark_expected({ data: [row("SO-1")] });
	const sort_removed = [];
	const make = () => {
		const attrs = {};
		const el = {
			attrs,
			attr: (key, value) => (value === undefined ? attrs[key] : ((attrs[key] = value), el)),
		};
		return el;
	};
	const pill = make();
	const cell = { find: () => pill, children: () => ({ first: () => pill }) };
	const checkbox = { attr: () => "SO-1" };
	const buttons = [];
	const columns = {};
	const column = (fieldname) => {
		const col = { length: 1, fieldname, has_button: false };
		col.find = (selector) => {
			if (selector === ".mi-legend-button") return { length: col.has_button ? 1 : 0 };
			if (selector === "[data-sort-by]") {
				const sortable = {
					removeAttr: (name) => (sort_removed.push([fieldname, name]), sortable),
				};
				return sortable;
			}
			return { length: 0, before: () => {}, };
		};
		col.append = (button) => {
			col.has_button = true;
			buttons.push([fieldname, button]);
		};
		return col;
	};
	global.$ = (arg) => {
		if (typeof arg === "string") {
			const button = { html: arg, on: (event, handler) => (button.handler = handler) };
			return button;
		}
		return {
			find: (selector) => (selector.includes("checkbox") ? checkbox : cell),
		};
	};
	const head = {
		find: (selector) => {
			const fieldname = /data-fieldname="([^"]+)"/.exec(selector)[1];
			columns[fieldname] = columns[fieldname] || column(fieldname);
			return columns[fieldname];
		},
	};
	const view = {
		data: [row("SO-1")],
		$result: {
			find: (selector) => (selector === ".list-row-head" ? head : { each: (fn) => fn.call({}) }),
		},
	};
	module_exports.decorate(view);
	module_exports.decorate(view);
	assert.match(pill.attrs.title, /Loading/);
	assert.deepEqual(buttons.map(([name]) => name), ["status_field", "custom_payment_display"]);
	assert.match(buttons[0][1].html, /aria-label="What do these states mean\?"/);
	assert.match(buttons[0][1].html, /min-width:24px/);
	// only the Payment header loses its sort (it holds no stored value)
	assert.ok(sort_removed.length > 0);
	assert.ok(sort_removed.every(([fieldname]) => fieldname === "custom_payment_display"));
	delete global.$;
});

test("ERPNext's own get_indicator and refresh still run when the new Status does not apply", async () => {
	const native = () => ["Closed", "green", "native"];
	let refreshed = 0;
	load();
	// ERPNext's own settings are present before our file loads
	global.frappe.listview_settings["Sales Order"] = {
		get_indicator: native,
		refresh: () => refreshed++,
		formatters: {},
	};
	const path = require.resolve("./sales_order_list.js");
	delete require.cache[path];
	require(path);
	const settings = global.frappe.listview_settings["Sales Order"];
	assert.notEqual(settings.get_indicator, native);
	assert.deepEqual(settings.get_indicator({ name: "SO-1", docstatus: 1, status: "Closed" }), ["Closed", "green", "native"]);
	assert.deepEqual(settings.get_indicator({ name: "SO-1", docstatus: 0, status: "Draft" }), ["Closed", "green", "native"]);
	global.frappe.call = () => Promise.resolve({ message: {} });
	await settings.refresh({ data: [] });
	assert.equal(refreshed, 1);
});

test("the indicator wrap is applied once", () => {
	load();
	const first = global.frappe.listview_settings["Sales Order"].get_indicator;
	const path = require.resolve("./sales_order_list.js");
	delete require.cache[path];
	require(path);
	assert.equal(global.frappe.listview_settings["Sales Order"].get_indicator, first);
});

// -- delivery countdown column ----------------------------------------------------------------

test("the countdown pill: green with 2+ days, orange tomorrow and today, red overdue, with the date as tooltip", () => {
	const { settings } = load();
	const format = settings.formatters.custom_earliest_delivery_date;
	const doc = marketplace_order();
	const pill = (color, text, date) => `<span class="indicator-pill ${color}" title="user:${date}">${text}</span>`;
	assert.equal(format("2026-11-09", {}, doc), pill("green", "In 5 days", "2026-11-09"));
	assert.equal(format("2026-11-06", {}, doc), pill("green", "In 2 days", "2026-11-06"));
	assert.equal(format("2026-11-05", {}, doc), pill("orange", "Tomorrow", "2026-11-05"));
	assert.equal(format("2026-11-04", {}, doc), pill("orange", "Today", "2026-11-04"));
	assert.equal(format("2026-11-03", {}, doc), pill("red", "Overdue 1 day", "2026-11-03"));
	assert.equal(format("2026-11-01", {}, doc), pill("red", "Overdue 3 days", "2026-11-01"));
});

test("the countdown shows for To Deliver and To Deliver and Bill only", () => {
	const { settings } = load();
	const format = settings.formatters.custom_earliest_delivery_date;
	for (const status of ["To Deliver", "To Deliver and Bill"]) {
		assert.match(format("2026-11-09", {}, marketplace_order({ status })), /In 5 days/, status);
	}
	for (const status of ["Closed", "Completed", "On Hold", "To Bill", "Draft", "Cancelled", undefined]) {
		assert.equal(format("2026-11-09", {}, marketplace_order({ status })), "", String(status));
	}
	assert.equal(format("2026-11-09", {}, marketplace_order({ docstatus: 0 })), "");
	assert.equal(format("2026-11-09", {}, marketplace_order({ docstatus: 2 })), "");
});

test("the countdown is empty without a date, a platform or a doc", () => {
	const { settings } = load();
	const format = settings.formatters.custom_earliest_delivery_date;
	for (const value of ["", null, undefined]) {
		assert.equal(format(value, {}, marketplace_order()), "", String(value));
	}
	assert.equal(format("2026-11-09", {}, marketplace_order({ custom_marketplace_platform: "" })), "");
	assert.equal(format("2026-11-09", {}), "");
});

test("the countdown escapes the tooltip date", () => {
	const { settings } = load();
	global.frappe.datetime.str_to_user = () => '"><script>';
	const html = settings.formatters.custom_earliest_delivery_date("2026-11-09", {}, marketplace_order());
	assert.ok(!html.includes("<script>"));
	assert.ok(html.includes("&quot;&gt;&lt;script&gt;"));
});

// -- bulk Delivery Note action ---------------------------------------------------------------

function load_bulk() {
	const listeners = [];
	global.frappe.realtime = { on: (event, handler) => listeners.push([event, handler]) };
	global.frappe.ui = { Dialog: class {} };
	global.frappe.msgprint = () => {};
	const path = require.resolve("./sales_order_bulk_delivery.js");
	delete require.cache[path];
	require(path);
	return { listeners };
}

test("onload runs ERPNext's onload with one Delivery Note item that opens our dialog, other items untouched", async () => {
	const native_calls = [];
	const original_onload = (view) => {
		view.page.add_action_item("Close", () => native_calls.push("close"));
		view.page.add_action_item("Delivery Note", () => native_calls.push("native delivery note"));
		view.page.add_action_item("Advance Payment", () => native_calls.push("advance"));
	};
	const { settings } = load({ original_onload });
	const { listeners } = load_bulk();
	const added = [];
	const page = { add_action_item: (label, click) => added.push({ label, click }) };
	const original_method = page.add_action_item;
	settings.onload({ page, get_checked_items: () => [] });
	await settle();
	assert.deepEqual(
		added.map((item) => item.label),
		["Close", "Delivery Note", "Advance Payment"]
	);
	added[0].click();
	added[2].click();
	assert.deepEqual(native_calls, ["close", "advance"]);
	// the Delivery Note item never runs ERPNext's callback
	added[1].click();
	assert.deepEqual(native_calls, ["close", "advance"]);
	assert.equal(page.add_action_item, original_method);
	assert.deepEqual(
		listeners.map(([event]) => event),
		["bulk_delivery_note_progress"]
	);
});

test("without the bulk helper ERPNext's Delivery Note action stays as it is", async () => {
	const native_calls = [];
	const original_onload = (view) =>
		view.page.add_action_item("Delivery Note", () => native_calls.push("native delivery note"));
	const { settings } = load({ original_onload });
	const added = [];
	settings.onload({ page: { add_action_item: (label, click) => added.push({ label, click }) } });
	await settle();
	added[0].click();
	assert.deepEqual(native_calls, ["native delivery note"]);
});
