const assert = require("node:assert/strict");
const test = require("node:test");

global.__ = (message, args) => {
	if (!args) return message;
	return message.replace(/\{(\d+)\}/g, (_, index) => args[index]);
};

global.frappe = {
	// Resolves synchronously, but only for get_erpnext_sync_status - the call
	// load_erpnext_sync_status() makes, whose own .then() is what adds the
	// async "Send to ERPNext"/"Update ERPNext Order" button - so
	// apply_platform_gating()'s full Talabat control set can be asserted by
	// name in a synchronous test, not just "more than one button got added".
	// get_item_presentation() (load_item_presentation()) is deliberately left
	// unresolved here: its own .then() calls render_item_presentation(),
	// which uses real DOM APIs (document.createElement, ...) that this plain
	// Node test environment has no jsdom-equivalent stand-in for - resolving
	// it would fail with "document is not defined", unrelated to whatever
	// this file's tests actually assert.
	call: (opts) => ({
		then: (resolve) => {
			if (opts && String(opts.method || "").includes("get_erpnext_sync_status") && typeof resolve === "function") {
				resolve({message: {status: "Not Synced"}});
			}
			return {then: () => {}};
		},
	}),
	ui: {form: {on: () => {}}},
	utils: {escape_html: (v) => v},
};

const {
	build_cart_update_items,
	get_status_indicator,
	TALABAT_STATUS_COLORS,
	ERPNEXT_SYNC_STATUS_COLORS,
	promotion_status_note,
	promotion_lines,
	parse_promotions,
	catalog_price_cell_text,
	price_difference_text,
	render_json_evidence_fields_as_escaped_text,
	add_generic_status_indicator,
	add_status_indicators,
	apply_platform_gating,
	JSON_EVIDENCE_FIELDS,
} = require("./marketplace_order.js");

function make_frm(doc) {
	return {
		doc,
		is_new: () => false,
		add_custom_button: (label) => {
			frm_calls.custom_buttons.push(label);
		},
		dashboard: {
			add_indicator: (label, color) => frm_calls.indicators.push({label, color}),
		},
		set_df_property: () => {},
		get_field: () => ({$wrapper: {empty: () => {}, append: () => {}}}),
		refresh_field: () => {},
	};
}

let frm_calls;

test("cart update preserves existing replacement and addition items on repeat", () => {
	const rows = [
		{
			external_item_id: "replacement-item",
			external_sku: "replacement-sku",
			external_status: "IN_CART",
			pricing_type: "UNIT",
			replaced_external_item_id: "original-item",
		},
		{
			external_item_id: "addition-item",
			external_sku: "addition-sku",
			external_status: "ADDITION",
			pricing_type: "UNIT",
		},
	];
	const values = {
		item_0_status: "IN_CART",
		item_0_quantity: 1,
		item_1_status: "ADDITION",
		item_1_quantity: 2,
	};
	const expected = [
		{
			_id: "replacement-item",
			sku: "replacement-sku",
			status: "IN_CART",
			pricing: {pricing_type: "UNIT", quantity: 1},
			replaced_id: "original-item",
		},
		{
			_id: "addition-item",
			sku: "addition-sku",
			status: "ADDITION",
			pricing: {pricing_type: "UNIT", quantity: 2},
		},
	];

	assert.deepEqual(build_cart_update_items(rows, values), expected);
	assert.deepEqual(build_cart_update_items(rows, values), expected);
});

test("Talabat and ERPNext status indicators use the agreed standard colors", () => {
	assert.deepEqual(get_status_indicator("RECEIVED", TALABAT_STATUS_COLORS), {label: "RECEIVED", color: "green"});
	assert.deepEqual(get_status_indicator("PREPARING", TALABAT_STATUS_COLORS), {label: "PREPARING", color: "orange"});
	assert.deepEqual(get_status_indicator("CANCELLED", TALABAT_STATUS_COLORS), {label: "CANCELLED", color: "red"});
	assert.deepEqual(get_status_indicator("UNAVAILABLE", TALABAT_STATUS_COLORS), {label: "UNAVAILABLE", color: "gray"});
	assert.deepEqual(get_status_indicator("Synced", ERPNEXT_SYNC_STATUS_COLORS), {label: "Synced", color: "green"});
	assert.deepEqual(get_status_indicator("Modified After Sync", ERPNEXT_SYNC_STATUS_COLORS), {label: "Modified After Sync", color: "orange"});
	assert.deepEqual(get_status_indicator("Sync Error", ERPNEXT_SYNC_STATUS_COLORS), {label: "Sync Error", color: "red"});
});

test("promotion_status AVAILABLE and every other value get distinct, non-inventive notes", () => {
	assert.equal(promotion_status_note("AVAILABLE"), "Talabat promotion detail is available and shown per item below.");

	const unavailable_note = promotion_status_note("UNAVAILABLE");
	assert.match(unavailable_note, /unavailable/i);
	assert.match(unavailable_note, /UNAVAILABLE/);
	assert.match(unavailable_note, /Sub Total and Order Total/);

	// #83 made promotion_status a raw Data field - an unrecognized future
	// value must fall into the same "unavailable" handling, not be treated
	// as a second recognized state.
	const unrecognized_note = promotion_status_note("SOME_FUTURE_VALUE");
	assert.match(unrecognized_note, /unavailable/i);
	assert.match(unrecognized_note, /SOME_FUTURE_VALUE/);

	const blank_note = promotion_status_note("");
	assert.match(blank_note, /unavailable/i);
});

test("promotion_status UNAVAILABLE never hides totals and never invents sponsor/funding", () => {
	const lines = promotion_lines([{name: "Malicious", sponsorships: [{sponsor: "VENDOR", amount: 5}]}], "UNAVAILABLE");
	assert.deepEqual(lines, ["Promotion detail unavailable"]);
});

test("AVAILABLE with no item promotions reports none applied, not an error", () => {
	assert.deepEqual(promotion_lines([], "AVAILABLE"), ["No promotion applied"]);
	assert.deepEqual(promotion_lines(null, "AVAILABLE"), ["No promotion applied"]);
});

test("promotion sponsorship renders as safe plain-text lines and labels only documented VENDOR sponsorship", () => {
	const promotions = [
		{
			name: "<script>alert(1)</script>",
			type: "PERCENTAGE",
			discount_amount: 10,
			sponsorships: [
				{sponsor: "VENDOR", amount: 7},
				{sponsor: "SOME_OTHER_SPONSOR", amount: 3},
			],
		},
	];

	const lines = promotion_lines(promotions, "AVAILABLE");

	assert.deepEqual(lines, [
		"<script>alert(1)</script> (PERCENTAGE): 10",
		"VENDOR-sponsored amount: 7",
		"SOME_OTHER_SPONSOR: 3",
	]);
	// Every line is a plain string with the raw text preserved verbatim -
	// callers must append these via textContent (see append_promotion_cell),
	// never innerHTML, so an external sponsor/name string can never execute
	// as markup even though it is never escaped as HTML text here.
	for (const line of lines) assert.equal(typeof line, "string");
});

test("parse_promotions tolerates a raw JSON string, an already-parsed array, and invalid/blank input", () => {
	assert.deepEqual(parse_promotions('[{"name": "Promo"}]'), [{name: "Promo"}]);
	assert.deepEqual(parse_promotions([{name: "Promo"}]), [{name: "Promo"}]);
	assert.deepEqual(parse_promotions(null), []);
	assert.deepEqual(parse_promotions(""), []);
	assert.deepEqual(parse_promotions("not json"), []);
});

test("catalog price cell shows the resolved value or an unavailable reason, never a fallback", () => {
	assert.equal(catalog_price_cell_text({current_catalog_price: 12.5}), "12.50");
	assert.equal(
		catalog_price_cell_text({current_catalog_price: null, catalog_price_unavailable_reason: "No current Item Price."}),
		"Catalog Price Unavailable: No current Item Price."
	);
	assert.equal(catalog_price_cell_text({}), "Catalog Price Unavailable: —");
});

test("catalog price cell formats a UOM-conversion float to 2 decimals like the diff columns", () => {
	// A UOM conversion (e.g. stock price * a non-integer conversion factor)
	// can produce a long floating-point tail - this must render the same way
	// as the diff columns (.toFixed(2)), never the raw unformatted float.
	assert.equal(catalog_price_cell_text({current_catalog_price: 30.299999999999997}), "30.30");
});

test("promotion_lines skips a non-object promotion element instead of throwing", () => {
	// Stored promotion JSON of [null] is a valid "preserved without
	// interpretation" shape per #83 - it must not break the entire item
	// table.
	assert.doesNotThrow(() => promotion_lines([null], "AVAILABLE"));
	assert.deepEqual(promotion_lines([null], "AVAILABLE"), []);

	assert.doesNotThrow(() => promotion_lines(["not-an-object", 5, null], "AVAILABLE"));
	assert.deepEqual(promotion_lines(["not-an-object", 5, null], "AVAILABLE"), []);
});

test("promotion_lines guards a non-array sponsorships value instead of throwing", () => {
	const promotions = [{name: "Promo", type: "FIXED", discount_amount: 5, sponsorships: {sponsor: "VENDOR", amount: 5}}];

	assert.doesNotThrow(() => promotion_lines(promotions, "AVAILABLE"));
	assert.deepEqual(promotion_lines(promotions, "AVAILABLE"), ["Promo (FIXED): 5"]);
});

test("price_difference_text never labels a difference as a discount and is blank-safe", () => {
	assert.equal(price_difference_text(15, 10), "+5.00");
	assert.equal(price_difference_text(10, 15), "-5.00");
	assert.equal(price_difference_text(10, 10), "0.00");
	assert.equal(price_difference_text(null, 10), "—");
	assert.equal(price_difference_text(10, undefined), "—");
});

// Minimal stand-in for frappe.utils.escape_html, matching its real escaping
// of the characters that matter for this test (<, >, &, ", ').
function fake_escape_html(value) {
	return String(value)
		.replace(/&/g, "&amp;")
		.replace(/</g, "&lt;")
		.replace(/>/g, "&gt;")
		.replace(/"/g, "&quot;")
		.replace(/'/g, "&#39;");
}

test("render_json_evidence_fields_as_escaped_text sets an escaping formatter on exactly the four new JSON fields", () => {
	global.frappe.meta = {
		docfield_map: {
			"Marketplace Order": {
				billing_address: {},
				shipping_address: {},
				payment_transactions: {},
				refunds: {},
				// A pre-existing Talabat JSON field must be left untouched -
				// #108's fix is scoped to only the four new fields, not a
				// blanket change to every JSON field on this doctype.
				discounts: {},
			},
		},
	};
	const previous_escape_html = global.frappe.utils.escape_html;
	global.frappe.utils.escape_html = fake_escape_html;

	render_json_evidence_fields_as_escaped_text();

	const docfield_map = global.frappe.meta.docfield_map["Marketplace Order"];
	for (const fieldname of JSON_EVIDENCE_FIELDS) {
		assert.equal(typeof docfield_map[fieldname].formatter, "function");
	}
	assert.equal(docfield_map.discounts.formatter, undefined);

	const malicious = '{"name": "<img src=x onerror=alert(1)>"}';
	const rendered = docfield_map.billing_address.formatter(malicious);
	assert.equal(rendered, `<pre>${fake_escape_html(malicious)}</pre>`);
	assert.doesNotMatch(rendered, /<img/);

	assert.equal(docfield_map.shipping_address.formatter(""), "");
	assert.equal(docfield_map.shipping_address.formatter(null), "");

	delete global.frappe.meta;
	global.frappe.utils.escape_html = previous_escape_html;
});

test("add_generic_status_indicator shows the Financial/Fulfillment indicator pair sourced from the two new status fields", () => {
	const indicators = [];
	const frm = {
		doc: {external_financial_status: "PAID", external_fulfillment_status: "FULFILLED"},
		dashboard: {add_indicator: (label, color) => indicators.push({label, color})},
	};

	add_generic_status_indicator(frm);

	assert.equal(indicators.length, 2);
	assert.equal(indicators[0].label, "Financial: PAID");
	assert.equal(indicators[1].label, "Fulfillment: FULFILLED");
});

test("add_generic_status_indicator handles blank financial/fulfillment status without inventing a value", () => {
	const indicators = [];
	const frm = {
		doc: {external_financial_status: "", external_fulfillment_status: ""},
		dashboard: {add_indicator: (label, color) => indicators.push({label, color})},
	};

	add_generic_status_indicator(frm);

	assert.equal(indicators[0].label, "Financial: Unavailable");
	assert.equal(indicators[1].label, "Fulfillment: Unavailable");
});

test("add_status_indicators escapes an untrusted external_status value before it reaches the Talabat indicator label", () => {
	// #108 converted external_status from a closed Select to a Data field, so
	// an out-of-range/malicious value now persists instead of being rejected
	// at save time - it must still be safely escaped on display.
	const indicators = [];
	const frm = {
		doc: {external_status: "<img src=x onerror=alert(1)>", erpnext_sync_status: "Not Synced"},
		dashboard: {add_indicator: (label, color) => indicators.push({label, color})},
	};
	const previous_escape_html = global.frappe.utils.escape_html;
	global.frappe.utils.escape_html = fake_escape_html;

	add_status_indicators(frm);

	assert.doesNotMatch(indicators[0].label, /<img/);
	assert.match(indicators[0].label, /&lt;img/);

	global.frappe.utils.escape_html = previous_escape_html;
});

test("add_generic_status_indicator escapes an untrusted status value before it reaches the indicator label", () => {
	const indicators = [];
	const frm = {
		doc: {external_financial_status: "<img src=x onerror=alert(1)>", external_fulfillment_status: "FULFILLED"},
		dashboard: {add_indicator: (label, color) => indicators.push({label, color})},
	};
	const previous_escape_html = global.frappe.utils.escape_html;
	global.frappe.utils.escape_html = fake_escape_html;

	add_generic_status_indicator(frm);

	assert.doesNotMatch(indicators[0].label, /<img/);
	assert.match(indicators[0].label, /&lt;img/);

	global.frappe.utils.escape_html = previous_escape_html;
});

// #108 decision 9: platform gating on the Marketplace Order form itself.
test("apply_platform_gating shows the full Talabat action set for a Talabat order", () => {
	frm_calls = {custom_buttons: [], indicators: []};
	const frm = make_frm({
		platform: "Talabat",
		external_status: "RECEIVED",
		transport_type: "VENDOR_DELIVERY",
		erpnext_sync_status: "Not Synced",
		items: [],
	});

	assert.doesNotThrow(() => apply_platform_gating(frm));

	// The exact expected Talabat control set for a RECEIVED,
	// VENDOR_DELIVERY order - asserted by name, not just a count, so a
	// regression that silently drops or renames one of these (e.g. the
	// async "Send to ERPNext" button added from load_erpnext_sync_status()'s
	// own .then()) is actually caught.
	// load_erpnext_sync_status()'s frappe.call resolves synchronously in this
	// test harness, so "Send to ERPNext" is added before the rest of
	// apply_platform_gating()'s Talabat branch runs - a real form load
	// resolves it asynchronously instead, so the real UI's button order can
	// differ; this test cares about the exact SET of buttons, not their
	// real-world order.
	assert.deepEqual(frm_calls.custom_buttons, [
		"Send to ERPNext",
		"Refresh from Talabat",
		"Update Cart",
		"Dispatch",
		"Cancel Order",
	]);
	// The Talabat + ERPNext dashboard indicators from add_status_indicators().
	assert.ok(frm_calls.indicators.some((indicator) => indicator.label === "Talabat: RECEIVED"));
	assert.ok(frm_calls.indicators.some((indicator) => indicator.label === "ERPNext: Not Synced"));
	// Never the Financial/Fulfillment indicator pair shown to non-Talabat
	// platforms.
	assert.ok(!frm_calls.indicators.some((indicator) => indicator.label.startsWith("Financial:")));
	assert.ok(!frm_calls.indicators.some((indicator) => indicator.label.startsWith("Fulfillment:")));
});

test("apply_platform_gating shows only the Refresh from Shopify control and the Financial/Fulfillment indicator pair for a Shopify order", () => {
	frm_calls = {custom_buttons: [], indicators: []};
	const frm = make_frm({
		platform: "Shopify",
		external_financial_status: "PAID",
		external_fulfillment_status: "FULFILLED",
	});

	apply_platform_gating(frm);

	assert.deepEqual(frm_calls.custom_buttons, ["Refresh from Shopify"]);
	assert.deepEqual(frm_calls.indicators, [
		{label: "Financial: PAID", color: "gray"},
		{label: "Fulfillment: FULFILLED", color: "gray"},
	]);
});

test("apply_platform_gating shows nothing at all for a blank/unrecognized platform (fails closed)", () => {
	frm_calls = {custom_buttons: [], indicators: []};
	const frm = make_frm({platform: "", external_status: "RECEIVED"});

	apply_platform_gating(frm);

	assert.deepEqual(frm_calls.custom_buttons, []);
	assert.deepEqual(frm_calls.indicators, []);

	frm_calls = {custom_buttons: [], indicators: []};
	const unknown_platform_frm = make_frm({platform: "SomeFuturePlatform"});
	apply_platform_gating(unknown_platform_frm);

	assert.deepEqual(frm_calls.custom_buttons, []);
	assert.deepEqual(frm_calls.indicators, []);
});
