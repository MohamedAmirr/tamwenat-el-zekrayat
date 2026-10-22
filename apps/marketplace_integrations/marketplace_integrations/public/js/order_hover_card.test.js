const assert = require("node:assert/strict");
const test = require("node:test");

global.__ = (message, args) => {
	if (!args) return message;
	return message.replace(/\{(\d+)\}/g, (_, index) => args[index]);
};

const SAMPLE = {
	sales_order: "SAL-ORD-1",
	customer: "Ahmed <b>",
	phone: "0100",
	status: "To Deliver and Bill",
	currency: "EGP",
	total: 150,
	items: [{ item_name: "Rice", qty: 2, uom: "Nos" }],
	more_items: false,
	marketplace: {
		platform: "Talabat",
		reference: "ORD-77",
		payment_status: "PAID",
		last_sync_error: "<img src=x onerror=alert(1)>",
		last_sync: { status: "Error" },
	},
	bosta: { status: "Out for delivery", tracking_number: "TRK-1" },
};

function fake_element() {
	return {
		innerHTML: "",
		children: [],
		querySelector() {
			return null;
		},
		appendChild(child) {
			this.children.push(child);
		},
		setAttribute() {},
		addEventListener(name, fn) {
			this.handlers = this.handlers || {};
			this.handlers[name] = fn;
		},
		style: {},
	};
}

function load({ hover_card = true } = {}) {
	const calls = [];
	const dialogs = [];
	const pending = [];
	const hover_cards = [];
	global.document = { createElement: fake_element };
	delete global.$;
	global.frappe = {
		utils: { escape_html: (value) => String(value).replace(/</g, "&lt;").replace(/>/g, "&gt;") },
		call: (opts) => {
			calls.push(opts);
			return new Promise((resolve, reject) => pending.push({ opts, resolve, reject }));
		},
		ui: {
			Dialog: class {
				constructor(config) {
					this.config = config;
					this.fields_dict = { details: { $wrapper: { append: (el) => (this.body = el) } } };
					dialogs.push(this);
				}
				show() {}
			},
		},
	};
	if (hover_card) {
		// Like the real one: the instance goes into jQuery data on the trigger (when jQuery exists).
		global.frappe.ui.hover_card = (trigger, opts) => {
			const instance = {
				closed: 0,
				destroyed: 0,
				close() {
					this.closed++;
				},
				destroy() {
					this.destroyed++;
					this.close();
				},
			};
			hover_cards.push({ row: trigger, trigger, opts, instance });
			if (typeof global.$ !== "function") return trigger;
			const $trigger = global.$(trigger);
			$trigger.data("es-hover-card", instance);
			return $trigger;
		};
	}
	const path = require.resolve("./order_hover_card.js");
	delete require.cache[path];
	const module_exports = require(path);
	const helper = module_exports.create({ method: "m.get", arg: "sales_order" });
	return { module_exports, helper, calls, dialogs, pending, hover_cards };
}

const tick = () => new Promise((resolve) => setTimeout(resolve, 0));

// A list row as Frappe renders it: the subject link carries the document name.
function row(name) {
	const el = fake_element();
	el.ownerDocument = { createElement: fake_element };
	el.nodeType = 1;
	el.matches = (selector) => selector === ".list-row-container";
	el.link = name ? { getAttribute: () => name, dataset: { name } } : null;
	el.querySelector = (selector) => {
		if (selector === ".list-subject a[data-name]") return el.link;
		return null;
	};
	return el;
}

// jQuery stand-in with real `.data()` semantics: values live per element, and `remove(el)` wipes
// them like jQuery.cleanData does when Frappe calls `.remove()` on a row (before any observer).
function install_jquery() {
	const document_handlers = {};
	const store = new Map();
	global.document.addEventListener = () => {};
	global.$ = (target) => {
		if (target === global.document) {
			return {
				on: (name, fn) => (document_handlers[name] = fn),
				off: (name, fn) => {
					if (document_handlers[name] === fn) delete document_handlers[name];
				},
			};
		}
		return {
			data: (key, value) => {
				if (value === undefined) return (store.get(target) || {})[key];
				store.set(target, { ...(store.get(target) || {}), [key]: value });
			},
		};
	};
	const remove = (el) => {
		store.delete(el);
		if (el.link) store.delete(el.link);
	};
	return { handlers: document_handlers, store, remove };
}

test("the card shows the readable platform number, escapes server text and totals", () => {
	const { module_exports } = load();
	const html = module_exports.render_sales_order_card(SAMPLE);
	assert.match(html, /ORD-77/);
	assert.doesNotMatch(html, /MKTORD-/);
	assert.match(html, /Rice x 2/);
	assert.match(html, /TRK-1/);
	assert.match(html, /Out for delivery/);
	assert.match(html, /Ahmed &lt;b&gt;/);
	assert.doesNotMatch(html, /<img/);
	assert.match(html, /&lt;img src=x onerror=alert\(1\)&gt;/);
});

test("parts the server left out are not rendered", () => {
	const { module_exports } = load();
	const html = module_exports.render_sales_order_card({ ...SAMPLE, marketplace: null, bosta: null });
	assert.doesNotMatch(html, /Marketplace|Delivery status|Last sync error/);
	assert.match(html, /Customer/);
});

test("fetch is lazy and cached: one request per order, however often it is shown", async () => {
	const { helper, calls, pending } = load();
	assert.equal(calls.length, 0);
	const a = fake_element();
	const first = helper.fill(a, "SO-1");
	assert.match(a.innerHTML, /role="status"/);
	pending[0].resolve({ message: SAMPLE });
	await first;
	assert.match(a.innerHTML, /SAL-ORD-1/);
	const b = fake_element();
	await helper.fill(b, "SO-1");
	assert.equal(calls.length, 1);
	assert.match(b.innerHTML, /SAL-ORD-1/);
	const other = helper.fill(fake_element(), "SO-2");
	assert.equal(calls.length, 2);
	pending[1].resolve({ message: SAMPLE });
	await other;
	assert.equal(calls[0].silent, true);
	assert.deepEqual(calls[0].args, { sales_order: "SO-1" });
});

test("a late response for a row that is no longer the one shown is ignored", async () => {
	const { helper, pending } = load();
	const first = fake_element();
	const second = fake_element();
	const p1 = helper.fill(first, "SO-1");
	const p2 = helper.fill(second, "SO-2");
	pending[1].resolve({ message: { ...SAMPLE, sales_order: "SO-2" } });
	await p2;
	pending[0].resolve({ message: { ...SAMPLE, sales_order: "SO-1" } });
	await p1;
	assert.match(second.innerHTML, /SO-2/);
	// The first surface is never overwritten after it was superseded; it keeps its skeleton.
	assert.match(first.innerHTML, /role="status"/);
	assert.doesNotMatch(second.innerHTML, /SO-1/);
});

test("an error shows the escaped server message and is not cached", async () => {
	const { helper, pending, calls } = load();
	const el = fake_element();
	const p = helper.fill(el, "SO-1");
	const message = JSON.stringify([JSON.stringify({ message: "<b>Not</b> permitted <script>x</script>" })]);
	pending[0].reject({ responseJSON: { _server_messages: message } });
	await p;
	assert.match(el.innerHTML, /role="alert"/);
	assert.doesNotMatch(el.innerHTML, /<b>|<script>/);
	assert.match(el.innerHTML, /Not permitted/);
	const retry = helper.fill(fake_element(), "SO-1");
	assert.equal(calls.length, 2);
	pending[1].resolve({ message: SAMPLE });
	await retry;
});

test("server_message falls back to a plain sentence", () => {
	const { module_exports } = load();
	assert.equal(module_exports.server_message({}), "Could not load the order details.");
});

test("attach adds a Details button and a hover card whose content is built lazily", async () => {
	const { helper, hover_cards, calls } = load();
	const el = row("SO-1");
	helper.attach(el, "SO-1");
	assert.equal(el.children.length, 1);
	assert.equal(el.children[0].textContent, "Details");
	assert.equal(el.children[0].type, "button");
	assert.equal(hover_cards.length, 1);
	// The trigger is the subject link, never the whole row: the card cannot cover the checkboxes.
	assert.equal(hover_cards[0].trigger, el.link);
	assert.notEqual(hover_cards[0].trigger, el);
	assert.equal(hover_cards[0].opts.side, "bottom");
	assert.equal(hover_cards[0].opts.align, "start");
	assert.equal(calls.length, 0);
	let repositioned = false;
	const content = hover_cards[0].opts.content({ onreposition: () => (repositioned = true) });
	assert.match(content.innerHTML, /role="status"/);
	assert.equal(calls.length, 1);
	assert.equal(repositioned, false);
});

test("the Details button opens the same content in a dialog and closes the hover card", async () => {
	const { helper, hover_cards, dialogs, pending } = load();
	const el = row("SO-1");
	helper.attach(el, "SO-1");
	let closed = false;
	hover_cards[0].opts.content({ close: () => (closed = true), onreposition() {} });
	const stopped = [];
	el.children[0].handlers.click({ preventDefault() {}, stopPropagation: () => stopped.push(true) });
	assert.ok(closed);
	assert.deepEqual(stopped, [true]);
	assert.equal(dialogs.length, 1);
	assert.equal(dialogs[0].config.title, "Order details");
	pending[0].resolve({ message: SAMPLE });
	await tick();
	await tick();
	assert.match(dialogs[0].body.innerHTML, /SAL-ORD-1/);
});

test("without frappe.ui.hover_card the Details button still works", () => {
	const { helper, hover_cards, dialogs } = load({ hover_card: false });
	const el = row("SO-1");
	helper.attach(el, "SO-1");
	assert.equal(hover_cards.length, 0);
	el.children[0].handlers.click({ preventDefault() {}, stopPropagation() {} });
	assert.equal(dialogs.length, 1);
});

test("attach_to_list handles each row once and ignores rows with no document name", () => {
	const { helper, hover_cards } = load();
	const rows = [row("SO-1"), row("")];
	const container = {
		querySelectorAll: () => rows,
	};
	const scan = helper.attach_to_list({ $result: [container] });
	scan();
	assert.equal(hover_cards.length, 1);
	assert.equal(helper.attach_to_list({ $result: [container] }), undefined);
});

test("a row without a subject link gets the Details button but no hover card", () => {
	const { helper, hover_cards } = load();
	const el = row("SO-1");
	el.link = null;
	helper.attach(el, "SO-1");
	assert.equal(hover_cards.length, 0);
	assert.equal(el.children.length, 1);
});

test("row_link only matches the subject link, so the checkbox column never triggers a card", () => {
	const { module_exports } = load();
	const seen = [];
	const el = { querySelector: (selector) => (seen.push(selector), null) };
	module_exports.row_link(el);
	assert.deepEqual(seen, [".list-subject a[data-name]"]);
});

function list_with(rows) {
	const container = { querySelectorAll: () => rows.slice() };
	return container;
}

test("a row removed by a re-render has its hover card destroyed, although jQuery data is wiped", () => {
	const { helper, hover_cards } = load();
	const { remove, store } = install_jquery();
	const el = row("SO-1");
	const rows = [el];
	const scan = helper.attach_to_list({ $result: [list_with(rows)] });
	assert.equal(hover_cards.length, 1);
	const { instance } = hover_cards[0];
	// The card is open; Frappe removes the row with jQuery .remove(), then the observer fires.
	remove(el);
	rows.length = 0;
	assert.equal(store.has(el.link), false);
	scan([{ removedNodes: [el] }]);
	assert.equal(instance.destroyed, 1);
	assert.equal(instance.closed, 1);
	// A second notification for the same row does not destroy twice.
	scan([{ removedNodes: [el] }]);
	assert.equal(instance.destroyed, 1);
});

test("a removed wrapper destroys the cards of the rows inside it", () => {
	const { helper, hover_cards } = load();
	const { remove } = install_jquery();
	const el = row("SO-1");
	const rows = [el];
	const scan = helper.attach_to_list({ $result: [list_with(rows)] });
	const wrapper = { nodeType: 1, matches: () => false, querySelectorAll: () => [el] };
	remove(el);
	rows.length = 0;
	scan([{ removedNodes: [wrapper] }, { removedNodes: [{ nodeType: 3 }] }]);
	assert.equal(hover_cards[0].instance.destroyed, 1);
});

test("a route change closes open cards without destroying them", () => {
	const { helper, hover_cards } = load();
	const { handlers } = install_jquery();
	const el = row("SO-1");
	const container = list_with([el]);
	container.isConnected = true;
	helper.attach_to_list({ $result: [container] });
	handlers["page-change"]();
	assert.equal(hover_cards[0].instance.closed, 1);
	assert.equal(hover_cards[0].instance.destroyed, 0);
	container.isConnected = false;
	handlers["page-change"]();
	assert.equal("page-change" in handlers, false);
});

test("a route change still closes the cards of rows whose jQuery data was wiped", () => {
	const { helper, hover_cards } = load();
	const { handlers, remove } = install_jquery();
	const el = row("SO-1");
	const container = list_with([el]);
	container.isConnected = true;
	helper.attach_to_list({ $result: [container] });
	remove(el);
	handlers["page-change"]();
	assert.equal(hover_cards[0].instance.closed, 1);
});

test("a route change with rows already removed does not touch their destroyed cards", () => {
	const { helper, hover_cards } = load();
	const { handlers, remove } = install_jquery();
	const el = row("SO-1");
	const rows = [el];
	const container = list_with(rows);
	container.isConnected = true;
	const scan = helper.attach_to_list({ $result: [container] });
	remove(el);
	rows.length = 0;
	scan([{ removedNodes: [el] }]);
	handlers["page-change"]();
	assert.equal(hover_cards[0].instance.destroyed, 1);
	assert.equal(hover_cards[0].instance.closed, 1);
});

test("a deleted order shows the server's own escaped message, not a framework dialog", async () => {
	const { helper, pending, calls } = load();
	const el = fake_element();
	const p = helper.fill(el, "SO-GONE");
	const message = JSON.stringify([JSON.stringify({ message: "Sales Order <i>SO-GONE</i> was not found." })]);
	// The endpoint raises ValidationError (HTTP 417): Desk shows nothing for a silent call.
	pending[0].reject({ status: 417, responseJSON: { _server_messages: message } });
	await p;
	assert.equal(calls[0].silent, true);
	assert.match(el.innerHTML, /role="alert"/);
	assert.match(el.innerHTML, /Sales Order SO-GONE was not found\./);
	assert.doesNotMatch(el.innerHTML, /<i>/);
});
