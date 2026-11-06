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
	items: [{ item_code: "ITEM-1", item_name: "Rice", qty: 2, image: "" }],
	marketplace: {
		platform: "Talabat",
		reference: "ORD-77",
		payment_status: "PAID",
		last_sync_error: "<img src=x onerror=alert(1)>",
		last_sync: { status: "Error" },
	},
	bosta: { summary: { "Out for delivery": 2, Delivered: 1 } },
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
		utils: { escape_html: (value) => String(value).replace(/&/g, "&amp;").replace(/</g, "&lt;").replace(/>/g, "&gt;") },
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
	assert.match(html, /Rice/);
	assert.match(html, /<bdi dir="ltr">x 2<\/bdi>/);
	assert.match(html, /Delivered: <bdi dir="ltr">1<\/bdi>/);
	assert.match(html, /Out for delivery/);
	assert.match(html, /Ahmed &lt;b&gt;/);
	assert.doesNotMatch(html, /<img/);
	assert.match(html, /&lt;img src=x onerror=alert\(1\)&gt;/);
});

function many_items(count) {
	return Array.from({ length: count }, (_, i) => ({
		item_code: `CODE-${i + 1}`,
		item_name: `Item ${i + 1}`,
		qty: i + 1,
		image: `https://cdn.example.com/${i + 1}.jpg`,
	}));
}

test("every item of a 45-item order is listed, in order, with count, code, quantity and a lazy image", () => {
	const { module_exports } = load();
	const html = module_exports.render_sales_order_card({ ...SAMPLE, items: many_items(45) });
	assert.match(html, /Items \(45\)/);
	assert.equal((html.match(/class="mi-hc-item"/g) || []).length, 45);
	assert.equal((html.match(/<img /g) || []).length, 45);
	assert.ok(html.indexOf("Item 1<") < html.indexOf("Item 2<"));
	assert.ok(html.indexOf("Item 44<") < html.indexOf("Item 45<"));
	assert.match(html, /<bdi dir="ltr">CODE-45<\/bdi>/);
	assert.match(html, /<bdi dir="ltr">x 45<\/bdi>/);
	assert.match(html, /loading="lazy"/);
	assert.match(html, /width="40" height="40"/);
	assert.doesNotMatch(html, /More items/);
});

test("the item area scrolls with a max height and keeps wheel scrolling inside (focusable for the Details dialog)", () => {
	const { module_exports } = load();
	const html = module_exports.render_sales_order_card({ ...SAMPLE, items: many_items(3) });
	assert.match(html, /class="mi-hc-items" tabindex="0"[^>]*max-height: 320px; overflow-y: auto; overscroll-behavior: contain/);
	assert.match(html, /aria-label="Items \(3\)"/);
});

test("an item without an image (or with an unsafe one) gets the neutral box and no img", () => {
	const { module_exports } = load();
	const items = [
		{ item_code: "A", item_name: "No image", qty: 1, image: "" },
		{ item_code: "B", item_name: "Missing key", qty: 1 },
		{ item_code: "C", item_name: "Script", qty: 1, image: "javascript:alert(1)" },
		{ item_code: "D", item_name: "Data", qty: 1, image: "data:image/png;base64,AAAA" },
		{ item_code: "E", item_name: "Quote", qty: 1, image: 'https://x.example/a.png" onerror="alert(1)' },
	];
	const html = module_exports.render_sales_order_card({ ...SAMPLE, items });
	assert.doesNotMatch(html, /<img/);
	assert.equal((html.match(/class="mi-hc-item"/g) || []).length, 5);
	assert.equal((html.match(/background: var\(--control-bg\)/g) || []).length >= 5, true);
});

test("safe_image allows http(s) and file paths only", () => {
	const { module_exports } = load();
	const { safe_image } = module_exports;
	assert.equal(safe_image("https://cdn.shopify.com/a.jpg?v=1"), "https://cdn.shopify.com/a.jpg?v=1");
	assert.equal(safe_image("/files/a.png"), "/files/a.png");
	assert.equal(safe_image("/private/files/a.png"), "/private/files/a.png");
	// Frappe keeps a space and an apostrophe in file names.
	assert.equal(safe_image("/files/Screenshot 2026-11-01.png"), "/files/Screenshot 2026-11-01.png");
	assert.equal(safe_image("/files/Mama's rice.jpg"), "/files/Mama's rice.jpg");
	const bad_values = [
		"",
		null,
		undefined,
		"//evil.example/a.png",
		"/assets/a.png",
		"ftp://x/a",
		"/files/../a",
		'/files/a".png',
		"/files/a\\b.png",
		"/files/a<b>.png",
		"/files/a\nb",
		"https://user:pw@example.com/a.png",
		"https://example.com/a b.png",
		"https://example.com/it's.png",
		"https:///a.png",
		"https://example.com/" + "a".repeat(2000),
	];
	for (const bad of bad_values) {
		assert.equal(safe_image(bad), "", String(bad));
	}
});

test("thumbnail_url asks Shopify's CDN for a small picture and leaves everything else alone", () => {
	const { module_exports } = load();
	const { thumbnail_url } = module_exports;
	const base = "https://cdn.shopify.com/s/files/1/0001/x.jpg";
	assert.equal(thumbnail_url(`${base}?v=1754753901`), `${base}?v=1754753901&width=96`);
	assert.equal(thumbnail_url(base), `${base}?width=96`);
	assert.equal(thumbnail_url(`${base}?v=1#frag`), `${base}?v=1&width=96#frag`);
	// An existing width is never doubled or replaced.
	assert.equal(thumbnail_url(`${base}?width=200&v=1`), `${base}?width=200&v=1`);
	assert.equal(thumbnail_url(`${base}?v=1&width=300`), `${base}?v=1&width=300`);
	// Other hosts (including look-alikes) and file paths are unchanged.
	assert.equal(thumbnail_url("https://cdn.example.com/a.jpg?v=1"), "https://cdn.example.com/a.jpg?v=1");
	assert.equal(thumbnail_url("https://cdn.shopify.com.evil.example/a.jpg"), "https://cdn.shopify.com.evil.example/a.jpg");
	assert.equal(thumbnail_url("https://evil.example/cdn.shopify.com/a.jpg"), "https://evil.example/cdn.shopify.com/a.jpg");
	assert.equal(thumbnail_url("/files/a.png"), "/files/a.png");
	assert.equal(thumbnail_url("/private/files/a.png"), "/private/files/a.png");
});

test("the rendered Shopify picture is the small version, other pictures are as stored", () => {
	const { module_exports } = load();
	const items = [
		{ item_code: "S", item_name: "Shopify", qty: 1, image: "https://cdn.shopify.com/s/files/1/x.jpg?v=9" },
		{ item_code: "F", item_name: "File", qty: 1, image: "/files/a.png" },
	];
	const html = module_exports.render_sales_order_card({ ...SAMPLE, items });
	assert.match(html, /src="https:\/\/cdn\.shopify\.com\/s\/files\/1\/x\.jpg\?v=9&amp;width=96"/);
	assert.match(html, /src="\/files\/a\.png"/);
});

test("item text and image URLs are escaped", () => {
	const { module_exports } = load();
	const items = [
		{
			item_code: "<i>C</i>",
			item_name: "<script>x</script>",
			qty: "<b>1</b>",
			image: "https://x.example/a.png?a=<b>",
		},
	];
	const html = module_exports.render_sales_order_card({ ...SAMPLE, items });
	assert.doesNotMatch(html, /<script>|<i>C|<b>1/);
	assert.match(html, /&lt;script&gt;x&lt;\/script&gt;/);
	// The unsafe URL (angle brackets) is dropped entirely.
	assert.doesNotMatch(html, /<img/);
});

test("codes and quantities are direction-isolated for RTL", () => {
	const { module_exports } = load();
	const html = module_exports.render_sales_order_card(SAMPLE);
	assert.match(html, /<bdi dir="ltr">ITEM-1<\/bdi>/);
	assert.doesNotMatch(html, /margin-left|margin-right|padding-left|padding-right|text-align: (left|right)/);
});

test("an order without items has no items section", () => {
	const { module_exports } = load();
	const html = module_exports.render_sales_order_card({ ...SAMPLE, items: [] });
	assert.doesNotMatch(html, /Items \(/);
});

test("a failed image hides itself through a listener, not an inline handler", () => {
	const { module_exports } = load();
	const html = module_exports.render_sales_order_card({ ...SAMPLE, marketplace: null, items: many_items(1) });
	assert.match(html, /<img /);
	assert.doesNotMatch(html, /onerror/i);
	const img = { style: {}, addEventListener: (name, fn) => (img.handler = { name, fn }) };
	module_exports.wire_images({ querySelectorAll: () => [img] });
	assert.equal(img.handler.name, "error");
	img.handler.fn();
	assert.equal(img.style.display, "none");
	// A root without querySelectorAll (skeleton, plain object) is ignored.
	module_exports.wire_images({});
	module_exports.wire_images(null);
});

test("fill wires the images after rendering the card", async () => {
	const { helper, pending } = load();
	const img = { style: {}, addEventListener: (name, fn) => (img.fn = fn) };
	const el = fake_element();
	el.querySelectorAll = () => [img];
	const p = helper.fill(el, "SO-1");
	pending[0].resolve({ message: { ...SAMPLE, items: many_items(1) } });
	await p;
	assert.match(el.innerHTML, /<img /);
	img.fn();
	assert.equal(img.style.display, "none");
});

test("the delivery address lines are escaped, direction-isolated and sit right after the customer", () => {
	const { module_exports } = load();
	const html = module_exports.render_sales_order_card({
		...SAMPLE,
		address: ["12 Nile St <b>", "القاهرة الجديدة", "<img src=x onerror=alert(1)>"],
	});
	assert.match(html, /Delivery Address/);
	assert.match(html, /<bdi dir="auto">12 Nile St &lt;b&gt;<\/bdi><br><bdi dir="auto">القاهرة الجديدة<\/bdi>/);
	assert.match(html, /overflow-wrap: anywhere/);
	assert.doesNotMatch(html, /<img |St <b>/);
	assert.ok(html.indexOf("Customer") < html.indexOf("Delivery Address"));
	assert.ok(html.indexOf("Delivery Address") < html.indexOf("Items ("));
});

test("the delivery address section is hidden when there are no lines", () => {
	const { module_exports } = load();
	for (const address of [undefined, null, [], ["", "  "], "not a list"]) {
		const html = module_exports.render_sales_order_card({ ...SAMPLE, address });
		assert.doesNotMatch(html, /Delivery Address/);
	}
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
