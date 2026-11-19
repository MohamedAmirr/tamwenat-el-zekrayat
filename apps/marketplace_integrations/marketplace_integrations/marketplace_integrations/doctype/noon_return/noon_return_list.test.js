const assert = require("node:assert/strict");
const test = require("node:test");

const realSetTimeout = global.setTimeout;
const escape_html = (text) =>
	String(text).replace(/&/g, "&amp;").replace(/</g, "&lt;").replace(/>/g, "&gt;").replace(/"/g, "&quot;");

let calls;
let call_handler;
let dialogs;
let msgprints;
let writable;

class FakeDialog {
	constructor(options) {
		this.options = options;
		this.values = {};
		this.df = {};
		this.primary_disabled = false;
		this.custom_actions = {};
		this.handlers = [];
		this.fields_dict = {};
		for (const field of options.fields) {
			this.values[field.fieldname] = field.default || "";
			this.df[field.fieldname] = { ...field };
			const entry = { html: "" };
			const set_html = (html) => {
				entry.html = html;
			};
			// The status region is a persistent child of the field wrapper: find() returns it.
			entry.$wrapper = { html: set_html, find: () => ({ html: set_html }) };
			this.fields_dict[field.fieldname] = entry;
		}
		this.$wrapper = {
			on: (event, selector, handler) => this.handlers.push({ event, selector, handler }),
		};
		dialogs.push(this);
	}

	show() {
		this.shown = true;
	}

	hide() {
		this.hidden = true;
		this.options.on_hide && this.options.on_hide();
	}

	add_custom_action(label, action) {
		this.custom_actions[label] = action;
	}

	get_value(fieldname) {
		return this.values[fieldname];
	}

	set_value(fieldname, value) {
		this.values[fieldname] = value;
	}

	set_df_property(fieldname, key, value) {
		this.df[fieldname][key] = value;
	}

	disable_primary_action() {
		this.primary_disabled = true;
	}

	enable_primary_action() {
		this.primary_disabled = false;
	}

	trigger(event, selector, target) {
		for (const entry of this.handlers) {
			if (entry.event === event && entry.selector === selector) entry.handler(target);
		}
	}
}

global.__ = (value, args) => (args ? value.replace(/\{(\d)\}/g, (_m, i) => args[Number(i)]) : value);
global.frappe = {
	utils: {
		escape_html,
		get_form_link: (doctype, name) => `<a href="/desk/${doctype}/${escape_html(name)}">${escape_html(name)}</a>`,
	},
	model: { can_write: () => writable },
	boot: {},
	ui: { Dialog: FakeDialog },
	db: { get_list: () => Promise.resolve([{ name: "ACC-1" }]) },
	call: (options) => {
		calls.push(options);
		return call_handler(options);
	},
	show_alert() {},
	msgprint(arg) {
		msgprints.push(arg);
	},
	listview_settings: {},
};

const list = require("./noon_return_list.js");

function reset() {
	calls = [];
	dialogs = [];
	msgprints = [];
	writable = true;
	call_handler = () => Promise.resolve({ message: {} });
}

const flush = () => new Promise((resolve) => realSetTimeout(resolve, 5));

// Poll ticks without waiting the real 2 s.
function fast_timers() {
	global.setTimeout = (fn) => realSetTimeout(fn, 0);
	return () => {
		global.setTimeout = realSetTimeout;
	};
}

const rows = [
	{ name: "NR-1", purchase_item_nr: "P1", partner_sku: "SKU1", order_nr: "O1", marketplace_order: "MO-1", inspection_outcome: "" },
	{ name: "NR-2", purchase_item_nr: "P2", partner_sku: "SKU2", order_nr: "O2", marketplace_order: "MO-2", inspection_outcome: "Sellable" },
	{ name: "NR-3", purchase_item_nr: "P3", partner_sku: "SKU3", order_nr: "O3", marketplace_order: "", inspection_outcome: "" },
];

test("the account select is hidden with one account and shown, starting blank, with several", () => {
	reset();
	let dialog = list.noon_return_list_build_dialog({ refresh() {} }, ["ACC-1"]);
	let field = dialog.options.fields.find((f) => f.fieldname === "account");
	assert.equal(field.hidden, 1);
	assert.equal(field.default, "ACC-1");

	dialog = list.noon_return_list_build_dialog({ refresh() {} }, ["ACC-1", "ACC-2"]);
	field = dialog.options.fields.find((f) => f.fieldname === "account");
	assert.equal(field.hidden, 0);
	assert.equal(field.default, "");
	assert.equal(field.options[0].value, "");
	assert.equal(field.options.length, 3);
});

test("a blank merchant code sends no merchant_code, a chosen one sends it", () => {
	assert.deepEqual(list.noon_return_list_lookup_args("ACC-1", " 123 ", ""), { account: "ACC-1", barcode: "123" });
	assert.ok(!("merchant_code" in list.noon_return_list_lookup_args("ACC-1", "123", "  ")));
	assert.deepEqual(list.noon_return_list_lookup_args("ACC-1", "123", "P1"), {
		account: "ACC-1",
		barcode: "123",
		merchant_code: "P1",
	});
});

test("polling resolves on done and on error", async () => {
	reset();
	let n = 0;
	call_handler = () => Promise.resolve({ message: { state: ++n < 3 ? "queued" : "done", rows: [] } });
	const done = await list.noon_return_list_poll("ACC-1", "k", () => true, { interval: 1, timeout: 1000 });
	assert.equal(done.state, "done");
	assert.equal(calls.length, 3);

	reset();
	call_handler = () => Promise.resolve({ message: { state: "error", error: "x" } });
	const failed = await list.noon_return_list_poll("ACC-1", "k", () => true, { interval: 1, timeout: 1000 });
	assert.equal(failed.state, "error");
	assert.equal(calls.length, 1);
});

test("polling stops with timeout when the lookup stays queued", async () => {
	reset();
	call_handler = () => Promise.resolve({ message: { state: "queued" } });
	const result = await list.noon_return_list_poll("ACC-1", "k", () => true, { interval: 1, timeout: 15 });
	assert.equal(result.state, "timeout");
	const seen = calls.length;
	await flush();
	assert.equal(calls.length, seen, "no poll after the timeout");
});

test("a superseded lookup stops polling and its result is ignored", async () => {
	reset();
	let current = true;
	call_handler = () => {
		current = false; // a newer lookup starts while this call is in flight
		return Promise.resolve({ message: { state: "done", rows } });
	};
	const result = await list.noon_return_list_poll("ACC-1", "k", () => current, { interval: 1, timeout: 1000 });
	assert.equal(result.state, "superseded");
	assert.equal(calls.length, 1);
});

test("a row with an outcome or without an order offers no choice", () => {
	assert.equal(list.noon_return_list_row_kind(rows[0]), "choice");
	assert.equal(list.noon_return_list_row_kind(rows[1]), "recorded");
	assert.equal(list.noon_return_list_row_kind(rows[2]), "unlinked");
	const html = list.noon_return_list_rows_html(rows);
	assert.equal((html.match(/<select/g) || []).length, 1);
	assert.match(html, /Already recorded: Restock/);
	assert.match(html, /Not linked to a Marketplace Order/);
	// Restock is the label of the stored value Sellable; the choice starts blank.
	assert.match(html, /<option value="Sellable">Restock<\/option>/);
	assert.match(html, /<option value="Damaged, Noon paid">Damaged, Noon paid<\/option>/);
	assert.match(html, /<option value=""><\/option>/);
});

test("only rows with a real choice are sent; blank is skipped", () => {
	assert.deepEqual(list.noon_return_list_collect(rows, { 0: "" }), []);
	assert.deepEqual(list.noon_return_list_collect(rows, { 0: "Damaged", 1: "Damaged", 2: "Damaged" }), [
		{ noon_return: "NR-1", outcome: "Damaged" },
	]);
	assert.deepEqual(list.noon_return_list_collect(rows, { 0: "bogus" }), []);
});

test("Noon text is escaped in rows and the lookup error is inserted as the server sent it", () => {
	const html = list.noon_return_list_rows_html([
		{ ...rows[0], purchase_item_nr: "<img src=x>", partner_sku: "<b>", order_nr: '"><script>' },
	]);
	assert.ok(!html.includes("<img"));
	assert.ok(!html.includes("<script>"));
	assert.match(html, /<bdi dir="ltr">&lt;img src=x&gt;<\/bdi>/);
	assert.match(list.noon_return_list_status_html("error", { error: "a &lt;b&gt; failed" }), /a &lt;b&gt; failed/);
	assert.ok(!list.noon_return_list_status_html("error", { error: "a &lt;b&gt; failed" }).includes("&amp;lt;"));
	assert.match(list.noon_return_list_status_html("error", {}), /data-action="retry"/);
	assert.match(list.noon_return_list_status_html("empty"), /Try another merchant code/);
	assert.match(list.noon_return_list_status_html("timeout"), /Still looking up/);
	assert.match(list.noon_return_list_status_html("loading"), /Looking up on Noon\.\.\./);
});

test("the summary keeps the server message as is, escapes names and outcomes, and points to the Credit Note", () => {
	const summary = list.noon_return_list_summary([
		{ noon_return: "NR-1", ok: true, outcome: "Sellable", sales_return: "DN-1", message: null },
		{ noon_return: "<NR-2>", ok: false, outcome: "<x>", sales_return: null, message: "refused &lt;why&gt;" },
	]);
	assert.equal(summary.indicator, "orange");
	assert.match(summary.message, /recorded as Restock/);
	assert.match(summary.message, /Draft Sales Return <a href="\/desk\/Delivery Note\/DN-1">/);
	assert.match(summary.message, /refused &lt;why&gt;/);
	assert.ok(!summary.message.includes("&amp;lt;"));
	assert.ok(!summary.message.includes("<NR-2>"));
	assert.match(summary.message, /Create the Credit Note from each Noon Return form\./);
	assert.equal(list.noon_return_list_summary([{ noon_return: "NR-1", ok: true, outcome: "Damaged" }]).indicator, "green");
});

test("the dialog: Confirm is disabled until a row has a choice, then applies and refreshes the list", async () => {
	reset();
	const restore = fast_timers();
	try {
		call_handler = (options) => {
			if (options.method.endsWith("lookup_return")) return Promise.resolve({ message: { queued: true, lookup_key: "k1" } });
			if (options.method.endsWith("get_lookup_result")) return Promise.resolve({ message: { state: "done", rows } });
			if (options.method.endsWith("apply_outcomes")) {
				return Promise.resolve({
					message: [{ noon_return: "NR-1", ok: true, outcome: "Damaged", sales_return: "DN-1", message: null }],
				});
			}
			return Promise.resolve({ message: [] });
		};
		let refreshed = false;
		const dialog = list.noon_return_list_build_dialog({ refresh: () => (refreshed = true) }, ["ACC-1"]);
		await flush();
		assert.equal(dialog.primary_disabled, true);

		dialog.values.barcode = "BC-1";
		await dialog.custom_actions["Look Up"]();
		assert.equal(dialog.primary_disabled, true, "no choice yet");
		const lookup = calls.find((c) => c.method.endsWith("lookup_return"));
		assert.deepEqual(lookup.args, { account: "ACC-1", barcode: "BC-1" });
		assert.match(dialog.fields_dict.results.html, /<select/);

		dialog.trigger("change", "select[data-row-index]", { target: { getAttribute: () => "0", value: "Damaged" } });
		assert.equal(dialog.primary_disabled, false);
		dialog.trigger("change", "select[data-row-index]", { target: { getAttribute: () => "0", value: "" } });
		assert.equal(dialog.primary_disabled, true);
		dialog.trigger("change", "select[data-row-index]", { target: { getAttribute: () => "0", value: "Damaged" } });

		await dialog.options.primary_action();
		const apply = calls.find((c) => c.method.endsWith("apply_outcomes"));
		assert.deepEqual(apply.args, { rows: [{ noon_return: "NR-1", outcome: "Damaged" }] });
		assert.equal(apply.freeze, true);
		assert.equal(dialog.hidden, true);
		assert.equal(refreshed, true);
		assert.match(msgprints.at(-1).message, /Draft Sales Return/);
	} finally {
		restore();
	}
});

test("the dialog shows the empty and error states and a lookup that returns no rows", async () => {
	reset();
	const restore = fast_timers();
	try {
		let result = { state: "done", rows: [] };
		call_handler = (options) => {
			if (options.method.endsWith("lookup_return")) return Promise.resolve({ message: { queued: true, lookup_key: "k" } });
			if (options.method.endsWith("get_lookup_result")) return Promise.resolve({ message: result });
			return Promise.resolve({ message: [] });
		};
		const dialog = list.noon_return_list_build_dialog({ refresh() {} }, ["ACC-1"]);
		dialog.values.barcode = "BC";
		await dialog.custom_actions["Look Up"]();
		assert.match(dialog.fields_dict.status.html, /no return items/);

		result = { state: "error", error: "Noon said &lt;no&gt;", rows: [] };
		await dialog.custom_actions["Look Up"]();
		assert.match(dialog.fields_dict.status.html, /Noon said &lt;no&gt;/);
		assert.match(dialog.fields_dict.status.html, /data-action="retry"/);
	} finally {
		restore();
	}
});

function lookup_handler({ results = {}, lookups = [] } = {}) {
	let n = 0;
	return (options) => {
		if (options.method.endsWith("lookup_return")) {
			lookups.push(options.args);
			return Promise.resolve({ message: { queued: true, lookup_key: `k${++n}` } });
		}
		if (options.method.endsWith("get_lookup_result")) {
			const result = results[options.args.lookup_key];
			return typeof result === "function" ? result() : Promise.resolve({ message: result || { state: "done", rows: [] } });
		}
		if (options.method.endsWith("apply_outcomes")) return Promise.resolve({ message: [] });
		return Promise.resolve({ message: [] });
	};
}

const enter = () => ({ key: "Enter", preventDefault() {}, stopPropagation() {} });

test("the dialog turns off Frappe's Enter-to-submit and focus, and focuses the barcode on page show", () => {
	reset();
	const dialog = list.noon_return_list_build_dialog({ refresh() {} }, ["ACC-1"]);
	assert.equal(dialog.options.no_submit_on_enter, true);
	assert.equal(dialog.options.no_focus, true);
	let focused = false;
	dialog.fields_dict.barcode.$input = { focus: () => (focused = true) };
	dialog.options.on_page_show();
	assert.equal(focused, true);
});

test("Enter on the barcode looks it up and does not Confirm", async () => {
	reset();
	const restore = fast_timers();
	try {
		call_handler = lookup_handler({ results: { k1: { state: "done", rows } } });
		const dialog = list.noon_return_list_build_dialog({ refresh() {} }, ["ACC-1"]);
		dialog.values.barcode = "BC-1";
		dialog.trigger("keydown", "input[data-fieldname='barcode']", enter());
		await flush();
		assert.ok(calls.some((c) => c.method.endsWith("lookup_return")));
		assert.ok(!calls.some((c) => c.method.endsWith("apply_outcomes")));
		assert.match(dialog.fields_dict.results.html, /<select/);
		// Another key does nothing.
		const before = calls.length;
		dialog.trigger("keydown", "input[data-fieldname='barcode']", { ...enter(), key: "a" });
		await flush();
		assert.equal(calls.length, before);
	} finally {
		restore();
	}
});

test("Confirm does nothing while the primary button is disabled, and cannot be applied twice", async () => {
	reset();
	const restore = fast_timers();
	try {
		call_handler = lookup_handler({ results: { k1: { state: "done", rows } } });
		const dialog = list.noon_return_list_build_dialog({ refresh() {} }, ["ACC-1"]);
		await dialog.options.primary_action(); // nothing looked up yet: disabled
		assert.ok(!calls.some((c) => c.method.endsWith("apply_outcomes")));

		dialog.values.barcode = "BC-1";
		await dialog.custom_actions["Look Up"]();
		await dialog.options.primary_action(); // rows but no choice: still disabled
		assert.ok(!calls.some((c) => c.method.endsWith("apply_outcomes")));

		dialog.trigger("change", "select[data-row-index]", { target: { getAttribute: () => "0", value: "Damaged" } });
		let release;
		call_handler = (options) =>
			options.method.endsWith("apply_outcomes")
				? new Promise((resolve) => {
						release = () => resolve({ message: [] });
					})
				: Promise.resolve({ message: [] });
		const first = dialog.options.primary_action();
		assert.equal(dialog.primary_disabled, true);
		await dialog.options.primary_action(); // in flight: ignored
		assert.equal(calls.filter((c) => c.method.endsWith("apply_outcomes")).length, 1);
		release();
		await first;
	} finally {
		restore();
	}
});

test("two racing lookups: only the newer one fills the dialog", async () => {
	reset();
	const restore = fast_timers();
	try {
		const first_rows = [{ ...rows[0], purchase_item_nr: "OLD-1" }];
		const second_rows = [{ ...rows[0], purchase_item_nr: "NEW-1" }];
		call_handler = lookup_handler({
			results: {
				k1: () => new Promise((resolve) => realSetTimeout(() => resolve({ message: { state: "done", rows: first_rows } }), 30)),
				k2: { state: "done", rows: second_rows },
			},
		});
		const dialog = list.noon_return_list_build_dialog({ refresh() {} }, ["ACC-1"]);
		dialog.values.barcode = "BC-1";
		const first = dialog.custom_actions["Look Up"]();
		dialog.values.barcode = "BC-2";
		const second = dialog.custom_actions["Look Up"]();
		await Promise.all([first, second]);
		await new Promise((resolve) => realSetTimeout(resolve, 50));
		assert.match(dialog.fields_dict.results.html, /NEW-1/);
		assert.ok(!dialog.fields_dict.results.html.includes("OLD-1"));
	} finally {
		restore();
	}
});

test("closing the dialog stops polling", async () => {
	reset();
	const restore = fast_timers();
	try {
		call_handler = lookup_handler({ results: { k1: { state: "queued" } } });
		const dialog = list.noon_return_list_build_dialog({ refresh() {} }, ["ACC-1"]);
		dialog.values.barcode = "BC-1";
		const pending = dialog.custom_actions["Look Up"]();
		await new Promise((resolve) => realSetTimeout(resolve, 20));
		dialog.options.on_hide();
		await pending;
		const seen = calls.length;
		await new Promise((resolve) => realSetTimeout(resolve, 20));
		assert.equal(calls.length, seen, "no poll after the dialog closed");
	} finally {
		restore();
	}
});

test("changing the account clears the rows and reloads that account's merchant codes", async () => {
	reset();
	const restore = fast_timers();
	try {
		call_handler = (options) => {
			if (options.method.endsWith("list_merchant_codes")) return Promise.resolve({ message: ["P1", "<P2>"] });
			return lookup_handler({ results: { k1: { state: "done", rows } } })(options);
		};
		const dialog = list.noon_return_list_build_dialog({ refresh() {} }, ["ACC-1", "ACC-2"]);
		dialog.values.account = "ACC-1";
		dialog.values.barcode = "BC-1";
		await dialog.custom_actions["Look Up"]();
		assert.match(dialog.fields_dict.results.html, /<select/);

		dialog.values.merchant_code = "P1";
		dialog.values.account = "ACC-2";
		dialog.options.fields.find((f) => f.fieldname === "account").onchange();
		await flush();
		const call = calls.find((c) => c.method.endsWith("list_merchant_codes"));
		assert.deepEqual(call.args, { account: "ACC-2" });
		assert.deepEqual(dialog.df.merchant_code.options.map((o) => o.value), ["", "P1", "<P2>"]);
		assert.equal(dialog.df.merchant_code.options[2].label, "&lt;P2&gt;");
		assert.equal(dialog.values.merchant_code, "");
		assert.ok(!/<select/.test(dialog.fields_dict.results.html), "rows cleared");
		assert.equal(dialog.primary_disabled, true);
	} finally {
		restore();
	}
});

test("editing the barcode after a lookup clears the rows and disables Confirm", async () => {
	reset();
	const restore = fast_timers();
	try {
		call_handler = lookup_handler({ results: { k1: { state: "done", rows } } });
		const dialog = list.noon_return_list_build_dialog({ refresh() {} }, ["ACC-1"]);
		dialog.values.barcode = "BC-1";
		await dialog.custom_actions["Look Up"]();
		dialog.trigger("change", "select[data-row-index]", { target: { getAttribute: () => "0", value: "Damaged" } });
		assert.equal(dialog.primary_disabled, false);

		dialog.trigger("input", "input[data-fieldname='barcode']", { target: { value: "BC-1" } });
		assert.equal(dialog.primary_disabled, false, "unchanged text keeps the rows");
		dialog.trigger("input", "input[data-fieldname='barcode']", { target: { value: "BC-12" } });
		assert.equal(dialog.primary_disabled, true);
		assert.ok(!/<select/.test(dialog.fields_dict.results.html));
		await dialog.options.primary_action();
		assert.ok(!calls.some((c) => c.method.endsWith("apply_outcomes")));
		// The barcode is not reqd, so Frappe's get_values() never shows Missing Fields for it.
		assert.ok(!dialog.options.fields.find((f) => f.fieldname === "barcode").reqd);
	} finally {
		restore();
	}
});

test("a rejected lookup_return shows its escaped server text; Retry only when it could pass", async () => {
	reset();
	const restore = fast_timers();
	try {
		const server = (status, message) => ({
			status,
			responseJSON: { _server_messages: JSON.stringify([JSON.stringify({ message })]) },
		});
		let failure = server(417, "<b>Bad</b> barcode a & b <img src=x>");
		call_handler = () => Promise.reject(failure);
		const dialog = list.noon_return_list_build_dialog({ refresh() {} }, ["ACC-1"]);
		dialog.values.barcode = "BC-1";
		await dialog.custom_actions["Look Up"]();
		let html = dialog.fields_dict.status.html;
		assert.match(html, /Bad barcode a &amp; b/); // tags stripped, the rest escaped
		assert.ok(!html.includes("<img"));
		assert.ok(!html.includes("<b>"));
		assert.ok(!html.includes('data-action="retry"'), "a validation refusal is not worth a Retry");

		failure = server(500, "boom");
		await dialog.custom_actions["Look Up"]();
		html = dialog.fields_dict.status.html;
		assert.match(html, /boom/);
		assert.match(html, /data-action="retry"/);

		failure = { status: 0 }; // network failure, no server text
		await dialog.custom_actions["Look Up"]();
		html = dialog.fields_dict.status.html;
		assert.match(html, /The lookup failed\./);
		assert.match(html, /data-action="retry"/);
	} finally {
		restore();
	}
});

test("the status is one persistent live region whose content only changes", () => {
	reset();
	const dialog = list.noon_return_list_build_dialog({ refresh() {} }, ["ACC-1"]);
	const region = dialog.options.fields.find((f) => f.fieldname === "status").options;
	assert.equal((region.match(/\srole="status"/g) || []).length, 1);
	assert.equal((region.match(/aria-live="polite"/g) || []).length, 1);
	assert.ok(!region.includes("alert"));
	assert.ok(!list.noon_return_list_status_html("error", {}).includes("role="));
});

test("an applied row's outcome and name are escaped in the summary", () => {
	const summary = list.noon_return_list_summary([
		{ noon_return: "<NR-1>", ok: true, outcome: "<img src=x>", sales_return: "<DN>", message: null },
	]);
	assert.ok(!summary.message.includes("<img"));
	assert.ok(!summary.message.includes("<NR-1>"));
	assert.match(summary.message, /&lt;img src=x&gt;/);
});

test("New Return replaces the Add action only for a user who can write both doctypes", () => {
	reset();
	const set_calls = [];
	const original = () => set_calls.push("original");
	const listview = {
		settings: {},
		set_primary_action: original,
		page: { set_primary_action: (...args) => set_calls.push(args) },
	};
	writable = false;
	list.noon_return_list_replace_add_action(listview);
	assert.equal(listview.set_primary_action, original);
	assert.deepEqual(set_calls, []);

	writable = true;
	list.noon_return_list_replace_add_action(listview);
	assert.equal(set_calls[0][0], "New Return");
	assert.equal(set_calls[0][1], listview.settings.primary_action);
	// Frappe redraws the action when rows are checked and unchecked: still New Return.
	listview.set_primary_action();
	assert.equal(set_calls.at(-1)[0], "New Return");
});

test("open_dialog explains when there is no readable enabled Noon account", async () => {
	reset();
	global.frappe.db.get_list = () => Promise.resolve([]);
	await list.noon_return_list_open_dialog({ refresh() {} });
	assert.equal(dialogs.length, 0);
	assert.match(msgprints.at(-1).message, /no enabled Noon Marketplace Account/);
});
