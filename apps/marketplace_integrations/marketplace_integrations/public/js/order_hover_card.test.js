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
		global.frappe.ui.hover_card = (row, opts) => {
			hover_cards.push({ row, opts });
			return row;
		};
	}
	const path = require.resolve("./order_hover_card.js");
	delete require.cache[path];
	const module_exports = require(path);
	const helper = module_exports.create({ method: "m.get", arg: "sales_order" });
	return { module_exports, helper, calls, dialogs, pending, hover_cards };
}

const tick = () => new Promise((resolve) => setTimeout(resolve, 0));

function row(name) {
	const el = fake_element();
	el.ownerDocument = { createElement: fake_element };
	el.querySelector = (selector) => {
		if (selector.includes("data-name") && selector.startsWith(".list-row-checkbox")) {
			return { getAttribute: () => name };
		}
		return null;
	};
	return el;
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
	rows[1].querySelector = () => null;
	const container = {
		querySelectorAll: () => rows,
	};
	const scan = helper.attach_to_list({ $result: [container] });
	scan();
	assert.equal(hover_cards.length, 1);
	assert.equal(helper.attach_to_list({ $result: [container] }), undefined);
});
