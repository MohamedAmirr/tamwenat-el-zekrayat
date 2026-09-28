// #108: this doctype is now shared with Shopify (and future platforms), not
// Talabat-only. Every Talabat-specific control (Refresh from Talabat, cart/
// dispatch/cancel actions, the Talabat dashboard indicator, item price/
// mapping presentation, and the ERPNext sync button) must keep running
// verbatim for a Talabat order, none of it must ever run for a Shopify
// order, and a blank/unrecognized platform must fail closed - show none of
// it - mirroring marketplace_product_mapping.js's platform gating (#106).
const JSON_EVIDENCE_FIELDS = ["billing_address", "shipping_address", "payment_transactions", "refunds"];

if (typeof frappe !== "undefined") {
	// Registered once, at doctype-client-script load time - i.e. before the
	// very first refresh_fields() call ever renders these fields for this
	// doctype in this browser session (Frappe fetches DocType meta and its
	// client script together, and evaluates the script only after the meta
	// is already cached - frappe.meta.docfield_map["Marketplace Order"] is
	// already populated by the time this line runs). Registering the
	// formatter later, inside the "refresh" form event, would be too late:
	// script_manager.trigger("refresh") always fires AFTER refresh_fields()
	// has already rendered the field once with the framework's own
	// (unescaped) default formatter for this same refresh pass - by then any
	// injected markup would already have had a chance to execute.
	render_json_evidence_fields_as_escaped_text();

	frappe.ui.form.on("Marketplace Order", {
		// frappe.meta.sync() can reload/replace this doctype's cached field
		// objects (e.g. after a metadata refresh), which silently drops the
		// formatter set once above at script-load time. onload fires before
		// this form instance's own refresh_fields() render (per form.js), so
		// re-asserting the formatter here is idempotent and keeps every
		// form-open in this session covered even after such a reload. This
		// still does not cover Report View, which renders these fields
		// without ever opening this form/onload at all - that gap is
		// tracked on the separate, pre-existing JSON-field-escaping issue,
		// not fixed here.
		onload(frm) {
			render_json_evidence_fields_as_escaped_text();
		},
		refresh(frm) {
			if (frm.is_new()) return;
			frm.set_df_property("items", "read_only", 1);
			frm.set_df_property("external_status", "read_only", 1);
			frm.set_df_property("transport_type", "read_only", 1);
			apply_platform_gating(frm);
		},
	});
}

// #108 decision 9: Talabat keeps every existing control verbatim (Refresh
// from Talabat, cart/dispatch/cancel actions, the Talabat + ERPNext status
// indicators, item price/mapping presentation, Send to ERPNext). Shopify
// shows the Financial/Fulfillment status indicator pair instead of Talabat's
// own status indicator, and its own item presentation (mapping only, not
// Talabat's fulfillment/pricing table) - but never Talabat's status
// indicator or cart/dispatch/cancel actions, none of which apply to Shopify.
// Shopify also gets the ERPNext sync button/status (#110 unblocks
// server-side sync for Shopify; #108 decision 7 only ever blocked it as a
// placeholder until #110 landed). Any other value (blank/unrecognized
// platform) shows none of the above at all, failing closed rather than
// guessing which control set applies - the same pattern
// marketplace_product_mapping.js uses (#106).
function apply_platform_gating(frm) {
	const platform = frm.doc.platform;
	if (platform === "Talabat") {
		add_status_indicators(frm);
		load_erpnext_sync_status(frm);

		frm.add_custom_button(__("Refresh from Talabat"), () => {
			frappe.call({
				method: "marketplace_integrations.api.talabat_orders.refresh_order",
				args: { marketplace_order: frm.doc.name },
				freeze: true,
				freeze_message: __("Refreshing from Talabat..."),
			}).then(() => frm.reload_doc());
		});
		add_order_actions(frm);

		load_item_presentation(frm);
	} else if (platform === "Shopify") {
		add_generic_status_indicator(frm);
		load_erpnext_sync_status(frm);
		frm.add_custom_button(__("Refresh from Shopify"), () => {
			frappe.call({
				method: "marketplace_integrations.api.shopify_orders.refresh_order",
				args: { marketplace_order: frm.doc.name },
				freeze: true,
				freeze_message: __("Refreshing from Shopify..."),
			}).then(() => frm.reload_doc());
		});
		load_shopify_item_presentation(frm);
	}
}

function load_shopify_item_presentation(frm) {
	frappe.call({
		method: "marketplace_integrations.api.marketplace_order_presentation.get_item_presentation",
		args: { marketplace_order: frm.doc.name },
	}).then((response) => {
		const wrapper = frm.get_field("talabat_order_presentation").$wrapper;
		wrapper.empty();
		for (const item of response.message.items || []) {
			const line = document.createElement("p");
			// A custom/no-variant line (variant: null on the Shopify side) has no
			// mapping to report at all - showing it as its own clean label rather
			// than concatenating with the "Unmapped"/mapped-item text below, which
			// always read "No Shopify variant: Unmapped" for this case even though
			// nothing was actually left unmapped.
			line.textContent = item.external_variant_id
				? `${item.external_variant_id}: ${item.mapped_item || __("Unmapped")}`
				: __("No Shopify variant");
			wrapper.append(line);
		}
	});
}

// Read-only JSON-fieldtype fields are not HTML-escaped by Frappe's default
// Desk formatter (confirmed live against this Frappe version's installed
// base_input.js/formatters.js: set_disp_area()'s escape whitelist covers
// Data/Long Text/Small Text/Text/Password/MultiSelect only, and the JSON
// fieldtype has no entry in frappe.form.formatters, so it silently falls
// back to the Data formatter - which returns the raw value unescaped - and
// is inserted via jQuery .html(). Only <script>/<style>/a few other
// specific tags are stripped afterwards (frappe.dom.remove_script_and_style)
// - an payload like `<img src=x onerror=...>` survives that and would
// execute. This already affects Talabat's own latest_payload/discounts/
// additional_fees/promotions/barcodes fields too (tracked separately, out of
// scope here per the locked plan) - the four fields #108 adds are escaped
// here explicitly rather than inheriting the same gap.
// Sets `formatter` directly on this doctype's cached meta docfields (not a
// per-form-instance control) - frappe.form.formatters.Data's own
// _apply_custom_formatter() (formatters.js) always re-reads
// frappe.meta.docfield_map[doctype][fieldname].formatter fresh, by name, on
// every single render, regardless of when it was set or which control
// instance is currently rendering - so setting it once here, globally,
// covers every Marketplace Order form render for these four fields for the
// rest of this browser session, since this registration runs from this form
// script. It does NOT cover the List View - that only loads
// marketplace_order_list.js, which never runs this registration, so a JSON
// column shown there still falls through to the unescaped Data formatter in
// list_view.js - nor does it cover Report View: that renderer does not go
// through frappe.form.formatters at all, so a Report View or List View built
// against one of these fields before any Marketplace Order form has ever
// been opened is not covered by this fix - both gaps (shared with Talabat's
// own pre-existing latest_payload/discounts/additional_fees/promotions/
// barcodes JSON fields) are tracked separately, not closed here. Re-applied
// again from an "onload" form handler below so
// a frappe.meta.sync() reload of this doctype's cached field objects can't
// silently drop it either.
function render_json_evidence_fields_as_escaped_text() {
	const docfield_map = frappe.meta && frappe.meta.docfield_map && frappe.meta.docfield_map["Marketplace Order"];
	if (!docfield_map) return;
	for (const fieldname of JSON_EVIDENCE_FIELDS) {
		const df = docfield_map[fieldname];
		if (!df) continue;
		df.formatter = (value) => (value ? `<pre>${frappe.utils.escape_html(value)}</pre>` : "");
	}
}

function load_erpnext_sync_status(frm) {
	frappe.call({
		method: "marketplace_integrations.api.marketplace_orders.get_erpnext_sync_status",
		args: {marketplace_order: frm.doc.name},
	}).then((response) => {
		frm.doc.erpnext_sync_status = response.message.status;
		frm.refresh_field("erpnext_sync_status");
		add_erpnext_sync_action(frm);
	});
}

function add_erpnext_sync_action(frm) {
	if (frm.doc.erpnext_sales_order && frm.doc.erpnext_sync_status === "Synced") return;
	const label = frm.doc.erpnext_sales_order ? __("Update ERPNext Order") : __("Send to ERPNext");
	frm.add_custom_button(label, () => {
		frappe.call({
			method: "marketplace_integrations.api.marketplace_orders.sync_to_erpnext",
			args: {marketplace_order: frm.doc.name},
			freeze: true,
			freeze_message: __("Synchronizing with ERPNext..."),
		}).then((response) => {
			if (!response.message.ok) {
				// response.message.error can interpolate Talabat-sourced values
				// (external_item_id/external_sku, ignore_xss_filter fields never
				// sanitized server-side) - frappe.msgprint's message renders as
				// HTML, so escape at the interpolation point per the pattern
				// already established elsewhere in this codebase (#84).
				frappe.msgprint({
					message: frappe.utils.escape_html(response.message.error),
					indicator: "red",
					title: __("ERPNext Sync Blocked"),
				});
			}
			frm.reload_doc();
		});
	});
}

const TALABAT_STATUS_COLORS = {
	RECEIVED: "green",
	ACCEPTED: "green",
	COMPLETED: "green",
	DELIVERED: "green",
	READY_FOR_PICKUP: "green",
	DISPATCHED: "green",
	PENDING: "orange",
	PREPARING: "orange",
	UPDATE_CART: "orange",
	CANCELLED: "red",
	REJECTED: "red",
	FAILED: "red",
};

const ERPNEXT_SYNC_STATUS_COLORS = {
	"Not Synced": "gray",
	Synced: "green",
	"Modified After Sync": "orange",
	"Sync Error": "red",
};

function get_status_indicator(status, color_by_status) {
	return {
		label: status || __("Unavailable"),
		color: color_by_status[status] || "gray",
	};
}

// A status VALUE (external_status/erpnext_sync_status/external_financial_status/
// external_fulfillment_status) is rendered into a dashboard indicator label
// below. frm.dashboard.add_indicator()/__() do no HTML escaping of their own
// - the label is inserted as markup - so any value that can carry arbitrary
// external text (external_status/the two new fields are plain Data fields,
// not a closed Select, specifically so an unrecognized future platform value
// persists instead of failing validation - see #108/this fix) must be run
// through frappe.utils.escape_html() at the interpolation point. This is a
// real Talabat behavior change: an out-of-range/malicious external_status
// string used to be REJECTED at save time back when this was a closed
// Select, and now persists (since #108 converted it to Data) - it is at
// least safely escaped on display after this fix, not silently rendered as
// markup.
function add_status_indicators(frm) {
	const talabat_status = get_status_indicator(frm.doc.external_status, TALABAT_STATUS_COLORS);
	const erpnext_sync_status = get_status_indicator(frm.doc.erpnext_sync_status, ERPNEXT_SYNC_STATUS_COLORS);
	frm.dashboard.add_indicator(__("Talabat: {0}", [frappe.utils.escape_html(talabat_status.label)]), talabat_status.color);
	frm.dashboard.add_indicator(__("ERPNext: {0}", [frappe.utils.escape_html(erpnext_sync_status.label)]), erpnext_sync_status.color);
}

// Non-Talabat platforms (Shopify, for now) get the Financial/Fulfillment
// status indicator pair #108's locked plan (decision 9) originally
// envisioned, sourced from the two generic status fields this fix adds
// (external_financial_status/external_fulfillment_status) - blank for
// Talabat, populated by #109 for Shopify. Same escaping requirement as
// add_status_indicators() above: these are plain Data fields carrying
// unvalidated external text.
function add_generic_status_indicator(frm) {
	const financial_status = get_status_indicator(frm.doc.external_financial_status, {});
	const fulfillment_status = get_status_indicator(frm.doc.external_fulfillment_status, {});
	frm.dashboard.add_indicator(
		__("Financial: {0}", [frappe.utils.escape_html(financial_status.label)]),
		financial_status.color
	);
	frm.dashboard.add_indicator(
		__("Fulfillment: {0}", [frappe.utils.escape_html(fulfillment_status.label)]),
		fulfillment_status.color
	);
}

function load_item_presentation(frm) {
	frappe.call({
		method: "marketplace_integrations.api.marketplace_order_presentation.get_item_presentation",
		args: { marketplace_order: frm.doc.name },
	}).then((response) => render_item_presentation(frm, response.message));
}

function add_order_actions(frm) {
	const status = frm.doc.external_status;
	if (["RECEIVED", "UPDATE_CART"].includes(status)) {
		frm.add_custom_button(__("Update Cart"), () => update_order(frm, "UPDATE_CART"));
	}
	if (!["CANCELLED", "DELIVERED", "DISPATCHED", "READY_FOR_PICKUP"].includes(status) && frm.doc.transport_type === "VENDOR_DELIVERY") {
		frm.add_custom_button(__("Dispatch"), () => update_order(frm, "DISPATCHED"));
	}
	if (!["CANCELLED", "DELIVERED", "DISPATCHED", "READY_FOR_PICKUP"].includes(status) && frm.doc.transport_type === "LOGISTICS_DELIVERY") {
		frm.add_custom_button(__("Ready for Pickup"), () => update_order(frm, "READY_FOR_PICKUP"));
	}
	if (!["CANCELLED", "DISPATCHED", "READY_FOR_PICKUP", "DELIVERED"].includes(status)) {
		frm.add_custom_button(__("Cancel Order"), () => {
			frappe.prompt(
				[{fieldname: "reason", fieldtype: "Select", label: __("Reason"), options: "CLOSED\nITEM_UNAVAILABLE\nTOO_BUSY", reqd: 1}],
				(values) => update_order(frm, "CANCELLED", values.reason),
				__("Cancel Talabat Order"),
				__("Submit")
			);
		});
	}
}

function update_order(frm, status, cancellation_reason) {
	if (status === "UPDATE_CART") {
		return show_cart_dialog(frm);
	}
	frappe.call({
		method: "marketplace_integrations.api.talabat_orders.update_order",
		args: {marketplace_order: frm.doc.name, status, cancellation_reason},
		freeze: true,
		freeze_message: __("Updating order on Talabat...")
	}).then(() => frm.reload_doc());
}

function show_cart_dialog(frm) {
	const fields = [];
	for (const [index, row] of (frm.doc.items || []).entries()) {
		fields.push({fieldtype: "Section Break", label: row.product_name || row.external_sku || row.external_item_id});
		const status_options = row.external_status === "ADDITION" ? "IN_CART\nNOT_FOUND\nREPLACED\nADDITION" : "IN_CART\nNOT_FOUND\nREPLACED";
		fields.push({fieldname: `item_${index}_status`, fieldtype: "Select", label: __("Status"), options: status_options, default: ["IN_CART", "NOT_FOUND", "REPLACED", "ADDITION"].includes(row.external_status) ? row.external_status : "IN_CART", reqd: 1});
		fields.push({fieldname: `item_${index}_quantity`, fieldtype: "Float", label: row.pricing_type === "KG" ? __("Weight") : __("Quantity"), default: row.pricing_type === "KG" ? row.fulfilled_weight : row.fulfilled_quantity, reqd: 1});
	}
	fields.push({fieldtype: "Section Break", label: __("Add Replacement or Item")});
	fields.push({fieldname: "new_sku", fieldtype: "Data", label: __("New Item SKU")});
	fields.push({fieldname: "new_status", fieldtype: "Select", label: __("New Item Status"), options: "ADDITION\nIN_CART", default: "ADDITION"});
	fields.push({fieldname: "new_replaced_id", fieldtype: "Data", label: __("Replace Item ID"), depends_on: "eval:doc.new_status == 'IN_CART'"});
	fields.push({fieldname: "new_quantity", fieldtype: "Float", label: __("New Quantity")});
	const dialog = new frappe.ui.Dialog({title: __("Update Talabat Cart"), fields, primary_action_label: __("Send to Talabat"), primary_action(values) {
		const items = build_cart_update_items(frm.doc.items || [], values);
		dialog.hide();
		frappe.call({method: "marketplace_integrations.api.talabat_orders.update_order", args: {marketplace_order: frm.doc.name, status: "UPDATE_CART", items: JSON.stringify(items)}, freeze: true, freeze_message: __("Updating cart on Talabat...")}).then(() => frm.reload_doc());
	}});
	dialog.show();
}

function build_cart_update_items(rows, values) {
	const items = rows.map((row, index) => {
		const pricing = {pricing_type: row.pricing_type};
		const item_status = values[`item_${index}_status`];
		if (!["NOT_FOUND", "REPLACED"].includes(item_status)) {
			if (row.pricing_type === "KG") pricing.weight = values[`item_${index}_quantity`];
			else pricing.quantity = values[`item_${index}_quantity`];
		}
		return {
			_id: row.external_item_id,
			sku: row.external_sku,
			status: item_status,
			pricing,
			...(item_status === "IN_CART" && row.replaced_external_item_id
				? {replaced_id: row.replaced_external_item_id}
				: {}),
		};
	});
	if (values.new_sku) {
		items.push({
			sku: values.new_sku,
			status: values.new_status || "ADDITION",
			pricing: {pricing_type: "UNIT", quantity: values.new_quantity || 0},
			...(values.new_status === "IN_CART" && values.new_replaced_id
				? {replaced_id: values.new_replaced_id}
				: {}),
		});
	}
	return items;
}

if (typeof module !== "undefined") {
	module.exports = {
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
	};
}

function render_item_presentation(frm, presentation) {
	const wrapper = frm.get_field("talabat_order_presentation").$wrapper;
	wrapper.empty();

	const reference_note = document.createElement("p");
	reference_note.textContent = __(
		"Current ERPNext Catalog Price below is a live current reference, not this order's historical pricing, and is never written back onto this order. ERPNext still calculates Sales Order prices separately when you send this order to ERPNext."
	);
	wrapper.append(reference_note);

	const promotion_note = document.createElement("p");
	promotion_note.textContent = promotion_status_note(frm.doc.promotion_status);
	wrapper.append(promotion_note);

	const by_item_id = new Map((presentation.items || []).map((item) => [item.external_item_id, item]));
	const table_wrapper = document.createElement("div");
	table_wrapper.className = "table-responsive";
	const table = document.createElement("table");
	table.className = "table table-bordered";
	table.setAttribute("aria-label", __("Talabat order items"));
	const caption = document.createElement("caption");
	caption.className = "sr-only";
	caption.textContent = __("Talabat order items with fulfillment, live mapping, and price comparison");
	table.append(caption);

	const headers = [
		__("Item"),
		__("SKU"),
		__("Status"),
		__("Mapping"),
		__("Replacement Item ID"),
		__("Pricing Type"),
		__("Original"),
		__("Fulfilled"),
		__("Talabat Original Unit / Total"),
		__("Talabat Fulfilled Unit / Total"),
		__("Original vs Fulfilled Diff (Unit / Total)"),
		__("Current ERPNext Catalog Price"),
		__("Talabat Order List Price"),
		__("Catalog vs List Price Diff"),
		__("Promotion"),
	];
	const header_row = document.createElement("tr");
	for (const header of headers) append_table_cell(header_row, header, "th");
	const thead = document.createElement("thead");
	thead.append(header_row);
	table.append(thead);

	const tbody = document.createElement("tbody");
	for (const row of frm.doc.items || []) {
		const mapping = by_item_id.get(row.external_item_id) || {};
		const item_row = document.createElement("tr");
		append_table_cell(item_row, row.product_name || __("Unnamed item"));
		append_table_cell(item_row, display_value(row.external_sku));
		append_status_cell(item_row, get_status_indicator(row.external_status, TALABAT_STATUS_COLORS));
		append_mapping_cell(item_row, mapping.mapped_item);
		append_table_cell(item_row, display_value(row.replaced_external_item_id));
		append_table_cell(item_row, display_value(row.pricing_type));
		append_table_cell(item_row, fulfillment_value(row, "original"));
		append_table_cell(item_row, fulfillment_value(row, "fulfilled"));
		append_table_cell(item_row, price_value(row.original_unit_price, row.original_total_price));
		append_table_cell(item_row, price_value(row.fulfilled_unit_price, row.fulfilled_total_price));
		append_table_cell(
			item_row,
			price_value(
				price_difference_text(row.fulfilled_unit_price, row.original_unit_price),
				price_difference_text(row.fulfilled_total_price, row.original_total_price)
			)
		);
		append_table_cell(item_row, catalog_price_cell_text(mapping));
		append_table_cell(item_row, display_value(row.original_list_price));
		append_table_cell(
			item_row,
			price_difference_text(mapping.current_catalog_price, row.original_list_price)
		);
		append_promotion_cell(item_row, parse_promotions(row.promotions), frm.doc.promotion_status);
		tbody.append(item_row);
	}
	table.append(tbody);
	table_wrapper.append(table);
	wrapper.append(table_wrapper);
}

function append_table_cell(table_row, value, tag_name = "td") {
	const cell = document.createElement(tag_name);
	cell.textContent = value;
	table_row.append(cell);
}

function append_status_cell(table_row, status) {
	const cell = document.createElement("td");
	const indicator = document.createElement("span");
	indicator.className = `indicator-pill ${status.color}`;
	indicator.textContent = status.label;
	cell.append(indicator);
	table_row.append(cell);
}

function append_mapping_cell(table_row, mapped_item) {
	const cell = document.createElement("td");
	const indicator = document.createElement("span");
	indicator.className = `indicator-pill ${mapped_item ? "green" : "gray"}`;
	indicator.textContent = mapped_item ? __("Mapped") : __("Unmapped");
	cell.append(indicator);
	if (mapped_item) cell.append(document.createTextNode(` ${mapped_item}`));
	table_row.append(cell);
}

function append_promotion_cell(table_row, promotions, promotion_status) {
	const cell = document.createElement("td");
	for (const line of promotion_lines(promotions, promotion_status)) {
		const line_element = document.createElement("div");
		line_element.textContent = line;
		cell.append(line_element);
	}
	table_row.append(cell);
}

function display_value(value) {
	return value === undefined || value === null || value === "" ? "—" : String(value);
}

function fulfillment_value(row, prefix) {
	const value = row.pricing_type === "KG" ? row[`${prefix}_weight`] : row[`${prefix}_quantity`];
	const unit = row.pricing_type === "KG" ? __("Weight") : __("Quantity");
	const weighted_pieces = row[`${prefix}_weighted_pieces`];
	const pieces = weighted_pieces === undefined || weighted_pieces === null || weighted_pieces === ""
		? ""
		: `; ${__("Weighted pieces")}: ${weighted_pieces}`;
	return `${unit}: ${display_value(value)}${pieces}`;
}

function price_value(unit_price, total_price) {
	return `${display_value(unit_price)} / ${display_value(total_price)}`;
}

// Any promotion_status other than exactly "AVAILABLE" - including blank and any
// unrecognized future value, since #83 made this a raw Talabat Data field rather
// than a closed enum - is treated as "promotion detail unavailable", per #84.
function promotion_status_note(promotion_status) {
	if (promotion_status === "AVAILABLE") {
		return __("Talabat promotion detail is available and shown per item below.");
	}
	return __(
		"Talabat promotion detail is unavailable for this order (status: {0}). Sub Total and Order Total in the Payment section remain accurate per Talabat.",
		[display_value(promotion_status)]
	);
}

// Promotion name/type/discount_amount/sponsorships are untrusted external
// (Talabat) text. This returns plain strings only - callers must render them
// with a text-node API (see append_promotion_cell above), never innerHTML.
function promotion_lines(promotions, promotion_status) {
	if (promotion_status !== "AVAILABLE") {
		return [__("Promotion detail unavailable")];
	}
	if (!Array.isArray(promotions) || promotions.length === 0) {
		return [__("No promotion applied")];
	}
	const lines = [];
	for (const promotion of promotions) {
		// Stored promotion JSON is untrusted external data preserved without
		// interpretation (#83) - a non-object element (e.g. `[null]`) must be
		// skipped rather than throwing and breaking the entire item table.
		if (typeof promotion !== "object" || promotion === null) continue;
		lines.push(
			`${display_value(promotion.name)} (${display_value(promotion.type)}): ${display_value(promotion.discount_amount)}`
		);
		const sponsorships = Array.isArray(promotion.sponsorships) ? promotion.sponsorships : [];
		for (const sponsorship of sponsorships) {
			if (typeof sponsorship !== "object" || sponsorship === null) continue;
			const sponsor = display_value(sponsorship.sponsor);
			const amount = display_value(sponsorship.amount);
			// Only Talabat's own documented VENDOR sponsorship value gets the
			// explicit "VENDOR-sponsored" label; any other sponsor value is
			// shown as-is rather than guessing it means PLATFORM/other funding.
			lines.push(sponsor === "VENDOR" ? __("VENDOR-sponsored amount: {0}", [amount]) : `${sponsor}: ${amount}`);
		}
	}
	return lines;
}

// A JSON-fieldtype grid value may arrive as a raw JSON string or as an
// already-parsed array depending on how the framework populated frm.doc for
// this session - handle both rather than assuming one.
function parse_promotions(raw) {
	if (!raw) return [];
	if (Array.isArray(raw)) return raw;
	try {
		const parsed = JSON.parse(raw);
		return Array.isArray(parsed) ? parsed : [];
	} catch (error) {
		return [];
	}
}

function catalog_price_cell_text(mapping) {
	if (typeof mapping.current_catalog_price === "number") {
		return mapping.current_catalog_price.toFixed(2);
	}
	return `${__("Catalog Price Unavailable")}: ${display_value(mapping.catalog_price_unavailable_reason)}`;
}

// Never labeled "discount" - #84 requires this only when Talabat data
// explicitly supports that interpretation, which this generic diff does not.
function price_difference_text(current, comparison) {
	if (typeof current !== "number" || typeof comparison !== "number") return "—";
	const diff = current - comparison;
	const sign = diff > 0 ? "+" : "";
	return `${sign}${diff.toFixed(2)}`;
}
