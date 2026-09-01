const assert = require("node:assert/strict");
const test = require("node:test");

const {
	build_cart_update_items,
	get_status_indicator,
	TALABAT_STATUS_COLORS,
	ERPNEXT_SYNC_STATUS_COLORS,
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
