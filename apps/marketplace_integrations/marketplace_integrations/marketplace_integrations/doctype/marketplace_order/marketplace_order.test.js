const assert = require("node:assert/strict");
const test = require("node:test");

global.__ = (message, args) => {
	if (!args) return message;
	return message.replace(/\{(\d+)\}/g, (_, index) => args[index]);
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
} = require("./marketplace_order.js");

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
