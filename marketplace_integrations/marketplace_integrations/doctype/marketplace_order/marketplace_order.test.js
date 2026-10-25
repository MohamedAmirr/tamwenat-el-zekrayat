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
			if (
				opts &&
				String(opts.method || "").includes("get_erpnext_sync_status") &&
				typeof resolve === "function"
			) {
				resolve({ message: { status: "Not Synced" } });
			}
			return { then: () => {} };
		},
	}),
	ui: { form: { on: () => {} } },
	utils: { escape_html: (v) => v },
};

const {
	add_clear_edit_marker_button,
	add_order_actions,
	build_cart_update_items,
	cancel_order,
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
	load_erpnext_sync_status,
	load_shopify_item_presentation,
	select_noon_fulfillment_buttons,
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
			add_indicator: (label, color) => frm_calls.indicators.push({ label, color }),
		},
		set_df_property: () => {},
		get_field: () => ({ $wrapper: { empty: () => {}, append: () => {} } }),
		refresh_field: () => {},
		refresh_fields: () => {},
		is_dirty: () => false,
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
			pricing: { pricing_type: "UNIT", quantity: 1 },
			replaced_id: "original-item",
		},
		{
			_id: "addition-item",
			sku: "addition-sku",
			status: "ADDITION",
			pricing: { pricing_type: "UNIT", quantity: 2 },
		},
	];

	assert.deepEqual(build_cart_update_items(rows, values), expected);
	assert.deepEqual(build_cart_update_items(rows, values), expected);
});

test("Talabat and ERPNext status indicators use the agreed standard colors", () => {
	assert.deepEqual(get_status_indicator("RECEIVED", TALABAT_STATUS_COLORS), {
		label: "RECEIVED",
		color: "green",
	});
	assert.deepEqual(get_status_indicator("PREPARING", TALABAT_STATUS_COLORS), {
		label: "PREPARING",
		color: "orange",
	});
	assert.deepEqual(get_status_indicator("CANCELLED", TALABAT_STATUS_COLORS), {
		label: "CANCELLED",
		color: "red",
	});
	assert.deepEqual(get_status_indicator("UNAVAILABLE", TALABAT_STATUS_COLORS), {
		label: "UNAVAILABLE",
		color: "gray",
	});
	assert.deepEqual(get_status_indicator("Synced", ERPNEXT_SYNC_STATUS_COLORS), {
		label: "Synced",
		color: "green",
	});
	assert.deepEqual(get_status_indicator("Modified After Sync", ERPNEXT_SYNC_STATUS_COLORS), {
		label: "Modified After Sync",
		color: "orange",
	});
	assert.deepEqual(get_status_indicator("Sync Error", ERPNEXT_SYNC_STATUS_COLORS), {
		label: "Sync Error",
		color: "red",
	});
	assert.deepEqual(get_status_indicator("Cancelled", ERPNEXT_SYNC_STATUS_COLORS), {
		label: "Cancelled",
		color: "gray",
	});
});

test("promotion_status AVAILABLE and every other value get distinct, non-inventive notes", () => {
	assert.equal(
		promotion_status_note("AVAILABLE"),
		"Talabat promotion detail is available and shown per item below."
	);

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
	const lines = promotion_lines(
		[{ name: "Malicious", sponsorships: [{ sponsor: "VENDOR", amount: 5 }] }],
		"UNAVAILABLE"
	);
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
				{ sponsor: "VENDOR", amount: 7 },
				{ sponsor: "SOME_OTHER_SPONSOR", amount: 3 },
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
	assert.deepEqual(parse_promotions('[{"name": "Promo"}]'), [{ name: "Promo" }]);
	assert.deepEqual(parse_promotions([{ name: "Promo" }]), [{ name: "Promo" }]);
	assert.deepEqual(parse_promotions(null), []);
	assert.deepEqual(parse_promotions(""), []);
	assert.deepEqual(parse_promotions("not json"), []);
});

test("catalog price cell shows the resolved value or an unavailable reason, never a fallback", () => {
	assert.equal(catalog_price_cell_text({ current_catalog_price: 12.5 }), "12.50");
	assert.equal(
		catalog_price_cell_text({
			current_catalog_price: null,
			catalog_price_unavailable_reason: "No current Item Price.",
		}),
		"Catalog Price Unavailable: No current Item Price."
	);
	assert.equal(catalog_price_cell_text({}), "Catalog Price Unavailable: —");
});

test("catalog price cell formats a UOM-conversion float to 2 decimals like the diff columns", () => {
	// A UOM conversion (e.g. stock price * a non-integer conversion factor)
	// can produce a long floating-point tail - this must render the same way
	// as the diff columns (.toFixed(2)), never the raw unformatted float.
	assert.equal(catalog_price_cell_text({ current_catalog_price: 30.299999999999997 }), "30.30");
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
	const promotions = [
		{
			name: "Promo",
			type: "FIXED",
			discount_amount: 5,
			sponsorships: { sponsor: "VENDOR", amount: 5 },
		},
	];

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
		doc: { external_financial_status: "PAID", external_fulfillment_status: "FULFILLED" },
		dashboard: { add_indicator: (label, color) => indicators.push({ label, color }) },
	};

	add_generic_status_indicator(frm);

	assert.equal(indicators.length, 2);
	assert.equal(indicators[0].label, "Financial: PAID");
	assert.equal(indicators[1].label, "Fulfillment: FULFILLED");
});

test("add_generic_status_indicator handles blank financial/fulfillment status without inventing a value", () => {
	const indicators = [];
	const frm = {
		doc: { external_financial_status: "", external_fulfillment_status: "" },
		dashboard: { add_indicator: (label, color) => indicators.push({ label, color }) },
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
		doc: {
			external_status: "<img src=x onerror=alert(1)>",
			erpnext_sync_status: "Not Synced",
		},
		dashboard: { add_indicator: (label, color) => indicators.push({ label, color }) },
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
		doc: {
			external_financial_status: "<img src=x onerror=alert(1)>",
			external_fulfillment_status: "FULFILLED",
		},
		dashboard: { add_indicator: (label, color) => indicators.push({ label, color }) },
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
	frm_calls = { custom_buttons: [], indicators: [] };
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
	assert.ok(
		!frm_calls.indicators.some((indicator) => indicator.label.startsWith("Fulfillment:"))
	);
});

test("apply_platform_gating shows only Refresh from Shopify and the indicator pair for an already-synced Shopify order", () => {
	frm_calls = { custom_buttons: [], indicators: [] };
	const frm = make_frm({
		platform: "Shopify",
		external_financial_status: "PAID",
		external_fulfillment_status: "FULFILLED",
		erpnext_sales_order: "SAL-ORD-2026-00001",
	});

	// Override the global frappe.call mock to return "Synced" for this test
	const original_call = global.frappe.call;
	global.frappe.call = (opts) => ({
		then: (resolve) => {
			if (
				opts &&
				String(opts.method || "").includes("get_erpnext_sync_status") &&
				typeof resolve === "function"
			) {
				resolve({ message: { status: "Synced" } });
			}
			return { then: () => {} };
		},
	});

	try {
		apply_platform_gating(frm);
	} finally {
		global.frappe.call = original_call;
	}

	assert.deepEqual(frm_calls.custom_buttons, ["Refresh from Shopify"]);
	assert.deepEqual(frm_calls.indicators, [
		{ label: "Financial: PAID", color: "gray" },
		{ label: "Fulfillment: FULFILLED", color: "gray" },
	]);
});

test("apply_platform_gating shows Send to ERPNext for an unsynced Shopify order", () => {
	frm_calls = { custom_buttons: [], indicators: [] };
	const frm = make_frm({
		platform: "Shopify",
		external_financial_status: "PAID",
		external_fulfillment_status: "FULFILLED",
	});

	apply_platform_gating(frm);

	assert.ok(frm_calls.custom_buttons.includes("Send to ERPNext"));
	assert.ok(frm_calls.custom_buttons.includes("Refresh from Shopify"));
});

function run_status_load({ saved, dirty }) {
	frm_calls = { custom_buttons: [], indicators: [] };
	const frm = make_frm({
		platform: "Shopify",
		name: "MO-1",
		modified: "2026-01-01 00:00:00",
		erpnext_sync_status: "Synced",
		erpnext_sales_order: "SAL-ORD-2026-00001",
	});
	frm.is_dirty = () => dirty;
	frm.reload_calls = 0;
	frm.reload_doc = () => {
		frm.reload_calls += 1;
	};
	const original_call = global.frappe.call;
	global.frappe.call = () => ({
		then: (resolve) => {
			resolve({
				message: { status: "Modified After Sync", last_sync_error: "changed", saved },
			});
			return { then: () => {} };
		},
	});
	try {
		load_erpnext_sync_status(frm);
	} finally {
		global.frappe.call = original_call;
	}
	return frm;
}

test("load_erpnext_sync_status reloads the form when the server saved and the form is clean", () => {
	const frm = run_status_load({ saved: true, dirty: false });
	assert.equal(frm.reload_calls, 1);
	assert.equal(frm.doc.modified, "2026-01-01 00:00:00");
	assert.equal(frm.doc.erpnext_sync_status, "Synced");
});

test("load_erpnext_sync_status applies status without reload or modified change when the server saved a dirty form", () => {
	const frm = run_status_load({ saved: true, dirty: true });
	assert.equal(frm.reload_calls, 0);
	assert.equal(frm.doc.modified, "2026-01-01 00:00:00");
	assert.equal(frm.doc.erpnext_sync_status, "Modified After Sync");
});

test("load_erpnext_sync_status applies status without reload when the server did not save", () => {
	for (const dirty of [false, true]) {
		const frm = run_status_load({ saved: false, dirty });
		assert.equal(frm.reload_calls, 0);
		assert.equal(frm.doc.modified, "2026-01-01 00:00:00");
		assert.equal(frm.doc.erpnext_sync_status, "Modified After Sync");
		assert.equal(frm.doc.last_sync_error, "changed");
	}
});

test("apply_platform_gating shows nothing at all for a blank/unrecognized platform (fails closed)", () => {
	frm_calls = { custom_buttons: [], indicators: [] };
	const frm = make_frm({ platform: "", external_status: "RECEIVED" });

	apply_platform_gating(frm);

	assert.deepEqual(frm_calls.custom_buttons, []);
	assert.deepEqual(frm_calls.indicators, []);

	frm_calls = { custom_buttons: [], indicators: [] };
	const unknown_platform_frm = make_frm({ platform: "SomeFuturePlatform" });
	apply_platform_gating(unknown_platform_frm);

	assert.deepEqual(frm_calls.custom_buttons, []);
	assert.deepEqual(frm_calls.indicators, []);
});

// #181: the Cancel Order button must set the echo-guard marker when a Sales Order is linked.
function run_cancel_order(doc, response) {
	const calls = [];
	const alerts = [];
	const frm = make_frm(doc);
	frm.reload_calls = 0;
	frm.reload_doc = () => {
		frm.reload_calls += 1;
	};
	const original_call = global.frappe.call;
	const original_show_alert = global.frappe.show_alert;
	global.frappe.call = (opts) => {
		calls.push(opts);
		return {
			then: (resolve) => {
				resolve({ message: response });
				return { then: () => {} };
			},
		};
	};
	global.frappe.show_alert = (opts) => alerts.push(opts);
	try {
		cancel_order(frm, "TOO_BUSY");
	} finally {
		global.frappe.call = original_call;
		global.frappe.show_alert = original_show_alert;
	}
	return { calls, alerts, frm };
}

test("cancel_order routes a linked order through request_platform_cancel with the Sales Order and reason", () => {
	const { calls, alerts, frm } = run_cancel_order(
		{ name: "MO-1", erpnext_sales_order: "SAL-ORD-1" },
		{ ok: true, already_requested: false, refreshed: true }
	);

	assert.equal(calls.length, 1);
	assert.equal(
		calls[0].method,
		"marketplace_integrations.api.marketplace_outbound.request_platform_cancel"
	);
	assert.deepEqual(calls[0].args, { sales_order: "SAL-ORD-1", cancellation_reason: "TOO_BUSY" });
	assert.deepEqual(alerts, []);
	assert.equal(frm.reload_calls, 1);
});

test("cancel_order tells the user when the cancellation was already requested or the refresh failed", () => {
	const already = run_cancel_order(
		{ name: "MO-1", erpnext_sales_order: "SAL-ORD-1" },
		{ ok: true, already_requested: true }
	);
	assert.equal(already.alerts.length, 1);
	assert.match(already.alerts[0].message, /already requested/);
	assert.equal(already.frm.reload_calls, 1);

	const not_refreshed = run_cancel_order(
		{ name: "MO-1", erpnext_sales_order: "SAL-ORD-1" },
		{ ok: true, already_requested: false, refreshed: false }
	);
	assert.equal(not_refreshed.alerts.length, 1);
	assert.match(not_refreshed.alerts[0].message, /could not be refreshed/);
	assert.equal(not_refreshed.frm.reload_calls, 1);
});

test("cancel_order keeps the direct Talabat update when no Sales Order is linked", () => {
	const { calls, alerts, frm } = run_cancel_order(
		{ name: "MO-1", erpnext_sales_order: "" },
		{ name: "MO-1" }
	);

	assert.equal(calls.length, 1);
	assert.equal(calls[0].method, "marketplace_integrations.api.talabat_orders.update_order");
	assert.deepEqual(calls[0].args, {
		marketplace_order: "MO-1",
		status: "CANCELLED",
		cancellation_reason: "TOO_BUSY",
	});
	assert.deepEqual(alerts, []);
	assert.equal(frm.reload_calls, 1);
});

test("add_order_actions shows Cancel Order only for RECEIVED and UPDATE_CART orders", () => {
	for (const status of ["RECEIVED", "UPDATE_CART"]) {
		frm_calls = { custom_buttons: [], indicators: [] };
		add_order_actions(make_frm({ external_status: status }));
		assert.ok(frm_calls.custom_buttons.includes("Cancel Order"), status);
	}
	for (const status of [
		"CANCELLED",
		"DISPATCHED",
		"READY_FOR_PICKUP",
		"DELIVERED",
		"SOMETHING_NEW",
		"",
		undefined,
	]) {
		frm_calls = { custom_buttons: [], indicators: [] };
		add_order_actions(make_frm({ external_status: status }));
		assert.ok(!frm_calls.custom_buttons.includes("Cancel Order"), String(status));
	}
});

test("select_noon_fulfillment_buttons keeps only available actions, or all when the call failed", () => {
	const buttons = [{ action: "create_shipment" }, { action: "retry" }, { action: "add_awb" }];
	assert.deepEqual(
		select_noon_fulfillment_buttons(buttons, ["retry", "unknown_action"]).map((b) => b.action),
		["retry"]
	);
	assert.deepEqual(select_noon_fulfillment_buttons(buttons, []), []);
	assert.equal(select_noon_fulfillment_buttons(buttons, null), buttons);
	assert.equal(select_noon_fulfillment_buttons(buttons, undefined), buttons);
});

test("Shopify item presentation shows Unmapped, a null variant label, mapped names, and keeps markup as text", async () => {
	const lines = [];
	const wrapper = { empty: () => (lines.length = 0), append: (line) => lines.push(line) };
	const original_call = global.frappe.call;
	const original_document = global.document;
	global.document = { createElement: () => ({ textContent: "" }) };
	global.frappe.call = () =>
		Promise.resolve({
			message: {
				items: [
					{ external_variant_id: "gid://shopify/ProductVariant/1", mapped_item: null },
					{ external_variant_id: null, mapped_item: null },
					{ external_variant_id: "gid://shopify/ProductVariant/3", mapped_item: "Item A" },
					{ external_variant_id: "gid://shopify/ProductVariant/4", mapped_item: "<img onerror=1>" },
				],
			},
		});
	try {
		await load_shopify_item_presentation({
			doc: { name: "MKTORD-1" },
			get_field: () => ({ $wrapper: wrapper }),
		});
	} finally {
		global.frappe.call = original_call;
		global.document = original_document;
	}

	assert.deepEqual(
		lines.map((line) => line.textContent),
		[
			"gid://shopify/ProductVariant/1: Unmapped",
			"No Shopify variant",
			"gid://shopify/ProductVariant/3: Item A",
			"gid://shopify/ProductVariant/4: <img onerror=1>",
		]
	);
});

test("Clear edit marker is offered only to a System Manager while a marker is set, behind a confirm", async () => {
	const original = { user: global.frappe.user, confirm: global.frappe.confirm, call: global.frappe.call, show_alert: global.frappe.show_alert };
	const calls = [];
	const alerts = [];
	let confirm_message;
	let confirm_yes;
	global.frappe.confirm = (message, yes) => {
		confirm_message = message;
		confirm_yes = yes;
	};
	global.frappe.show_alert = (alert) => alerts.push(alert);
	global.frappe.call = (opts) => {
		calls.push(opts);
		return Promise.resolve({ message: { ok: true } });
	};
	const buttons = [];
	const make = (doc) => ({
		doc,
		add_custom_button: (label, action) => buttons.push({ label, action }),
		reload_doc: () => Promise.resolve(),
	});
	try {
		global.frappe.user = { has_role: (role) => role === "System Manager" };
		add_clear_edit_marker_button(make({ name: "MO-1" }));
		assert.equal(buttons.length, 0, "no marker, no button");

		global.frappe.user = { has_role: () => false };
		add_clear_edit_marker_button(make({ name: "MO-1", outbound_edit_fingerprint: "abc" }));
		assert.equal(buttons.length, 0, "not a System Manager, no button");

		global.frappe.user = { has_role: (role) => role === "System Manager" };
		add_clear_edit_marker_button(make({ name: "MO-1", outbound_edit_fingerprint: "abc" }));
		assert.deepEqual(buttons.map((b) => b.label), ["Clear edit marker"]);

		buttons[0].action();
		assert.match(confirm_message, /Clear the marker/);
		assert.equal(calls.length, 0, "nothing is sent before the confirm");
		confirm_yes();
		await new Promise((resolve) => setTimeout(resolve, 5));
		assert.equal(calls.length, 1);
		assert.match(calls[0].method, /clear_shopify_edit_marker$/);
		assert.deepEqual(calls[0].args, { marketplace_order: "MO-1" });
		assert.equal(alerts[0].indicator, "green");
	} finally {
		Object.assign(global.frappe, original);
	}
});
