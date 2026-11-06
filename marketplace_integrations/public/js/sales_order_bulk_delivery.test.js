const assert = require("node:assert/strict");
const test = require("node:test");

global.__ = (message, args) => {
	if (!args) return message;
	return message.replace(/\{(\d+)\}/g, (_, index) => args[index]);
};

const ESCAPE_MAPPING = { "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;", "`": "&#x60;", "=": "&#x3D;" };

function load({ call = () => Promise.resolve({ message: { queued: true, count: 1, run_id: "run1" } }) } = {}) {
	const log = { dialogs: [], calls: [], alerts: [], msgprints: [], listeners: {}, copied: [] };
	class Dialog {
		constructor(config) {
			this.config = config;
			this.shown = false;
			this.hidden = false;
			this.primary_disabled = false;
			this.html = "";
			this.secondary = null;
			this.fields_dict = { progress: { $wrapper: { html: (value) => (this.html = value) } } };
			log.dialogs.push(this);
		}
		show() {
			this.shown = true;
		}
		hide() {
			this.hidden = true;
			if (this.config.on_hide) this.config.on_hide();
		}
		disable_primary_action() {
			this.primary_disabled = true;
		}
		enable_primary_action() {
			this.primary_disabled = false;
		}
		set_secondary_action_label(label) {
			this.secondary = { label };
		}
		set_secondary_action(fn) {
			this.secondary.fn = fn;
		}
		get_secondary_btn() {
			return {
				hide: () => (this.secondary = null),
				show: () => {},
			};
		}
	}
	global.frappe = {
		utils: {
			escape_html: (value) => String(value).replace(/[&<>"'`=]/g, (char) => ESCAPE_MAPPING[char] || char),
			copy_to_clipboard: (text) => log.copied.push(text),
		},
		router: { slug: (name) => name.toLowerCase().replace(/ /g, "-") },
		ui: { Dialog },
		msgprint: (message) => log.msgprints.push(message),
		show_alert: (options, seconds) => log.alerts.push({ options, seconds }),
		call: (options) => {
			log.calls.push(options);
			return call(options);
		},
		realtime: { on: (event, handler) => (log.listeners[event] = [...(log.listeners[event] || []), handler]) },
	};
	const path = require.resolve("./sales_order_bulk_delivery.js");
	delete require.cache[path];
	const api = require(path);
	return { api, log };
}

function listview(names, refreshed = []) {
	const added = [];
	const page = {
		add_action_item(label, click, standard) {
			added.push({ label, click, standard });
			return label;
		},
	};
	return { page, added, get_checked_items: () => names, refresh: () => refreshed.push(true) };
}

test("the helper registers itself on the shared namespace", () => {
	const { api } = load();
	assert.equal(global.frappe.marketplace_integrations.bulk_delivery, api);
});

// -- the Actions menu -----------------------------------------------------------------------

test("exactly one Delivery Note item is added, it opens our dialog, and there is no Delivery Note (ERPNext) item", () => {
	const { api, log } = load();
	const view = listview(["SO-1"]);
	const close = () => "close";
	const native_calls = [];
	const native = () => native_calls.push("native");
	api.run_with_replaced_action(view, () => {
		view.page.add_action_item("Close", close);
		view.page.add_action_item("Delivery Note", native, true);
	});
	assert.deepEqual(
		view.added.map((item) => item.label),
		["Close", "Delivery Note"]
	);
	assert.ok(!view.added.some((item) => item.label === "Delivery Note (ERPNext)"));
	assert.equal(view.added[0].click, close);
	assert.notEqual(view.added[1].click, native);
	assert.equal(view.added[1].standard, true);
	view.added[1].click();
	assert.deepEqual(native_calls, []);
	assert.equal(log.dialogs.length, 1);
	assert.ok(log.dialogs[0].shown);
});

test("the page method is restored afterwards, also when ERPNext's onload throws", () => {
	const { api } = load();
	const view = listview(["SO-1"]);
	const original = view.page.add_action_item;
	assert.throws(() =>
		api.run_with_replaced_action(view, () => {
			throw new Error("boom");
		})
	);
	assert.equal(view.page.add_action_item, original);
	assert.ok(Object.prototype.hasOwnProperty.call(view.page, "add_action_item"));
});

test("a page whose method comes from the prototype gets no own property left behind", () => {
	const { api } = load();
	const proto = { add_action_item() {} };
	const page = Object.create(proto);
	api.run_with_replaced_action({ page }, () => page.add_action_item("Delivery Note", () => {}));
	assert.ok(!Object.prototype.hasOwnProperty.call(page, "add_action_item"));
});

test("pressing our item opens the dialog", () => {
	const { api, log } = load();
	const view = listview(["SO-1", "SO-2"]);
	api.run_with_replaced_action(view, () => view.page.add_action_item("Delivery Note", () => "native"));
	view.added[0].click();
	assert.equal(log.dialogs.length, 1);
	assert.ok(log.dialogs[0].shown);
});

// -- dialog -------------------------------------------------------------------------------

test("nothing selected shows a message and no dialog", () => {
	const { api, log } = load();
	assert.equal(api.open_dialog(listview([])), null);
	assert.deepEqual(log.msgprints, ["Select at least one Sales Order."]);
	assert.equal(log.dialogs.length, 0);
});

test("more than 50 orders are refused before the dialog", () => {
	const { api, log } = load();
	const names = Array.from({ length: 51 }, (_, i) => `SO-${i}`);
	assert.equal(api.open_dialog(listview(names)), null);
	assert.deepEqual(log.msgprints, ["Select at most 50 Sales Orders at a time. You selected 51."]);
	assert.equal(log.dialogs.length, 0);
});

test("the dialog has one required shipping method Select with Bosta first and no COD field", () => {
	const { api, log } = load();
	api.open_dialog(listview(["SO-1", "SO-2"]));
	const config = log.dialogs[0].config;
	const select = config.fields.find((field) => field.fieldname === "shipping_method");
	assert.equal(select.fieldtype, "Select");
	assert.equal(select.options, "Bosta");
	assert.equal(select.default, "Bosta");
	assert.equal(select.reqd, 1);
	assert.ok(!config.fields.some((field) => /cod/i.test(field.fieldname)));
	assert.match(config.fields.find((field) => field.fieldtype === "HTML").options, /Sales Orders selected: 2/);
});

test("Create sends the selected names and the method, nothing else, then opens the progress dialog", async () => {
	const { api, log } = load();
	api.open_dialog(listview(["SO-1", "SO-2"]));
	const options = log.dialogs[0];
	await options.config.primary_action({ shipping_method: "Bosta" });
	assert.equal(log.calls.length, 1);
	assert.equal(log.calls[0].method, "marketplace_integrations.api.bulk_delivery_notes.create_delivery_notes");
	assert.equal(log.calls[0].type, "POST");
	assert.deepEqual(log.calls[0].args, { sales_orders: ["SO-1", "SO-2"], shipping_method: "Bosta" });
	assert.ok(log.calls[0].freeze);
	assert.ok(options.hidden);
	const progress = log.dialogs[1];
	assert.ok(progress.shown);
	assert.match(progress.html, /Creating documents 0 of 2/);
	assert.match(progress.html, /indicator-pill grey">Waiting/);
});

test("a run that was not queued shows the server message and keeps the dialog", async () => {
	const { api, log } = load({ call: () => Promise.resolve({ message: { queued: false, message: "still running" } }) });
	api.open_dialog(listview(["SO-1"]));
	const options = log.dialogs[0];
	await options.config.primary_action({ shipping_method: "Bosta" });
	assert.deepEqual(log.msgprints, ["still running"]);
	assert.ok(!options.hidden);
	assert.ok(!options.primary_disabled);
	assert.equal(log.dialogs.length, 1);
});

test("a failed request keeps the dialog open and the button usable", async () => {
	const { api, log } = load({ call: () => Promise.reject(new Error("refused")) });
	api.open_dialog(listview(["SO-1"]));
	const options = log.dialogs[0];
	await options.config.primary_action({ shipping_method: "Bosta" });
	assert.ok(!options.hidden);
	assert.ok(!options.primary_disabled);
	assert.equal(log.dialogs.length, 1);
});

// -- progress stages --------------------------------------------------------------------------

const row = (sales_order, status, extra = {}) => ({ sales_order, status, message: "", ...extra });

test("the creating stage shows N of M and the per-order states", () => {
	const { api, log } = load();
	api.on_progress({
		run_id: "r1",
		shipping_method: "Bosta",
		stage: "creating",
		total: 3,
		processed: 1,
		rows: [row("SO-1", "sent"), row("SO-2", "creating"), row("SO-3", "waiting")],
	});
	const html = log.dialogs[0].html;
	assert.match(html, /Creating documents 1 of 3/);
	assert.match(html, /indicator-pill orange">Waiting for Bosta/);
	assert.match(html, /indicator-pill blue">Creating documents</);
	assert.match(html, /indicator-pill grey">Waiting</);
	assert.match(html, /width: 17%/);
});

test("the waiting stage shows K of M accepted and moves the bar into the second half", () => {
	const { api, log } = load();
	api.on_progress({
		run_id: "r1",
		shipping_method: "Bosta",
		stage: "waiting",
		total: 2,
		sent_total: 2,
		accepted: 1,
		rows: [row("SO-1", "accepted"), row("SO-2", "sent")],
	});
	const html = log.dialogs[0].html;
	assert.match(html, /Waiting for Bosta: 1 of 2 accepted/);
	assert.match(html, /indicator-pill green">Accepted by Bosta/);
	assert.match(html, /width: 75%/);
});

test("later events update the same dialog instead of opening another", () => {
	const { api, log } = load();
	const base = { run_id: "r1", shipping_method: "Bosta", total: 1, rows: [row("SO-1", "sent")] };
	api.on_progress({ ...base, stage: "creating", processed: 1 });
	api.on_progress({ ...base, stage: "waiting", sent_total: 1, accepted: 0 });
	assert.equal(log.dialogs.length, 1);
	assert.match(log.dialogs[0].html, /Waiting for Bosta: 0 of 1 accepted/);
});

test("a run the person closed stays closed until its summary arrives, on any page", () => {
	const { api, log } = load();
	const base = { run_id: "r1", shipping_method: "Bosta", total: 1, rows: [row("SO-1", "sent")] };
	api.on_progress({ ...base, stage: "creating", processed: 1 });
	log.dialogs[0].hide();
	api.on_progress({ ...base, stage: "waiting", sent_total: 1, accepted: 0 });
	assert.equal(log.dialogs.length, 1);
	api.on_progress({
		...base,
		stage: "done",
		rows: [row("SO-1", "accepted")],
		summary: { accepted: 1, failed: 0, skipped: 0, still_creating: 0 },
	});
	assert.equal(log.dialogs.length, 2);
	assert.match(log.dialogs[1].html, /1 accepted, 0 failed, 0 skipped, 0 still creating/);
});

test("a run seen only through its events (after a reload) opens the dialog by itself", () => {
	const { api, log } = load();
	api.on_progress({
		run_id: "other",
		shipping_method: "Bosta",
		stage: "waiting",
		total: 1,
		sent_total: 1,
		accepted: 0,
		rows: [row("SO-1", "sent")],
	});
	assert.equal(log.dialogs.length, 1);
	assert.ok(log.dialogs[0].shown);
});

// -- summary ---------------------------------------------------------------------------------

const DONE = {
	run_id: "r1",
	shipping_method: "Bosta",
	stage: "done",
	total: 4,
	summary: { accepted: 1, failed: 1, skipped: 1, still_creating: 1 },
	rows: [
		row("SO-1", "accepted", { delivery_note: "DN-1", sales_invoice: "SINV-1" }),
		row("SO-2", "failed", { message: "Bosta did not accept the shipment: bad city", delivery_note: "DN-2", sales_invoice: "SINV-2" }),
		row("SO-3", "skipped", { message: "Delivery Note DN-9 already exists for this order." }),
		row("SO-4", "still_creating", { message: "Still creating at Bosta. Check the Delivery Note list later.", delivery_note: "DN-4" }),
	],
};

test("the summary counts each outcome, shows reasons and links the left documents", () => {
	const { api, log } = load();
	api.on_progress(DONE);
	const html = log.dialogs[0].html;
	assert.match(html, /1 accepted, 1 failed, 1 skipped, 1 still creating/);
	assert.match(html, /indicator-pill red">Failed/);
	assert.match(html, /indicator-pill orange">Skipped/);
	assert.match(html, /indicator-pill orange">Still creating at Bosta/);
	assert.match(html, /bad city/);
	assert.match(html, /href="\/app\/delivery-note\/DN-2"/);
	assert.match(html, /href="\/app\/sales-invoice\/SINV-2"/);
	assert.match(html, /width: 100%/);
	assert.match(html, /saved in your notifications/);
});

test("the copy button lists only the failed, skipped and unfinished orders with their documents", () => {
	const { api, log } = load();
	api.on_progress(DONE);
	const dialog = log.dialogs[0];
	assert.equal(dialog.secondary.label, "Copy failed orders");
	dialog.secondary.fn();
	assert.equal(log.copied.length, 1);
	const lines = log.copied[0].split("\n");
	assert.equal(lines.length, 3);
	assert.ok(!log.copied[0].includes("SO-1"));
	assert.match(lines[0], /^SO-2: Bosta did not accept the shipment: bad city \(Delivery Note DN-2, Sales Invoice SINV-2\)$/);
	assert.match(lines[2], /^SO-4: .*\(Delivery Note DN-4\)$/);
	assert.equal(log.alerts.length, 1);
});

test("no copy button when everything was accepted", () => {
	const { api, log } = load();
	api.on_progress({
		...DONE,
		rows: [row("SO-1", "accepted")],
		summary: { accepted: 1, failed: 0, skipped: 0, still_creating: 0 },
	});
	assert.equal(log.dialogs[0].secondary, null);
	assert.ok(!log.dialogs[0].html.includes("saved in your notifications"));
});

test("the summary refreshes the list when one is open", () => {
	const refreshed = [];
	const { api } = load();
	api.open_dialog(listview(["SO-1"], refreshed));
	api.on_progress(DONE);
	assert.equal(refreshed.length, 1);
});

test("server text and document names are escaped", () => {
	const { api, log } = load();
	api.on_progress({
		run_id: "r1",
		shipping_method: "Bosta",
		stage: "done",
		total: 1,
		summary: { accepted: 0, failed: 1, skipped: 0, still_creating: 0 },
		rows: [row('SO"><b>', "failed", { message: "<script>x</script>", delivery_note: "DN<1>" })],
	});
	const html = log.dialogs[0].html;
	assert.ok(!html.includes("<script>"));
	assert.ok(!html.includes('"><b>'));
	assert.ok(!html.includes("DN<1>"));
	assert.match(html, /&lt;script&gt;x&lt;\/script&gt;/);
});

test("an empty run says so", () => {
	const { api } = load();
	assert.match(api.render_progress({ stage: "done", rows: [] }), /No orders were processed/);
	assert.match(api.render_progress(undefined), /No orders were processed/);
});

// -- listener --------------------------------------------------------------------------------

test("listen registers the progress handler once per session", () => {
	const { api, log } = load();
	api.listen();
	api.listen();
	assert.equal(log.listeners.bulk_delivery_note_progress.length, 1);
	assert.equal(log.listeners.bulk_delivery_note_progress[0], api.on_progress);
});

test("listen waits while Frappe exposes a realtime socket that does not exist yet, then registers once", () => {
	const { api, log } = load();
	global.frappe.realtime.socket = null;
	assert.equal(api.listen(), false);
	assert.equal(log.listeners.bulk_delivery_note_progress, undefined);
	global.frappe.realtime.socket = {};
	assert.equal(api.listen(), true);
	assert.equal(api.listen(), false);
	assert.equal(log.listeners.bulk_delivery_note_progress.length, 1);
});

test("listen does nothing without frappe.realtime", () => {
	const { api } = load();
	delete global.frappe.realtime;
	assert.equal(api.listen(), false);
});

test("a new run in a reused dialog hides the previous run's Copy button", () => {
	const { api, log } = load();
	api.on_progress(DONE);
	const dialog = log.dialogs[0];
	assert.ok(dialog.secondary);
	api.on_progress({ run_id: "r2", shipping_method: "Bosta", stage: "creating", total: 1, processed: 0, rows: [row("SO-9", "waiting")] });
	assert.equal(log.dialogs.length, 1);
	assert.equal(dialog.secondary, null);
});
