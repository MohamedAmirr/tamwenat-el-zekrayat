// Copyright (c) 2026, Tamwenat El Zekrayat and contributors
// For license information, please see license.txt

// #167 Marketplace Channel Status - colors the "Channel Status" cell by
// meaning (green/orange/red/grey) and makes the count cells link to the
// relevant filtered list, so a merchant never has to open a technical log to
// tell whether a channel needs attention. Every dynamic value interpolated
// into HTML here goes through frappe.utils.escape_html() first, per
// docs/frappe-conventions.md's client-script escaping rule.

const MARKETPLACE_CHANNEL_STATUS_LEVEL_COLORS = {
	OK: "green",
	Warning: "orange",
	Error: "red",
	Disabled: "grey",
};

frappe.query_reports["Marketplace Channel Status"] = {
	filters: [],
	formatter(value, row, column, data, default_formatter) {
		value = default_formatter(value, row, column, data);

		if (column.fieldname === "level") {
			const color = MARKETPLACE_CHANNEL_STATUS_LEVEL_COLORS[data.level] || "grey";
			return `<span style="color: ${color}; font-weight: bold;">${frappe.utils.escape_html(
				__(data.level || "")
			)}</span>`;
		}

		if (column.fieldname === "orders_needing_attention" && data.orders_needing_attention) {
			return wrap_count_link(value, orders_needing_attention_url(data.account, data.platform));
		}

		if (column.fieldname === "product_sync_errors" && data.product_sync_errors) {
			return wrap_count_link(value, product_sync_errors_url(data.account));
		}

		if (column.fieldname === "import_errors" && data.import_errors) {
			return wrap_count_link(value, import_errors_url(data.account));
		}

		if (column.fieldname === "fulfillment_errors" && data.fulfillment_errors) {
			return wrap_count_link(value, needs_attention_url(data.account));
		}

		return value;
	},
};

function wrap_count_link(value, href) {
	return `<a href="${frappe.utils.escape_html(href)}">${value}</a>`;
}

// The order and import counts come from the same definition as the Marketplace
// Needs Attention report (#169), so both link to that report for the account.
function needs_attention_url(account) {
	return `/app/query-report/Marketplace%20Needs%20Attention?${to_query_string({ account })}`;
}

// Bosta is a carrier, not a Marketplace Account: its row has no account, so it links by platform.
function orders_needing_attention_url(account, platform) {
	if (!account && platform === "Bosta") {
		return `/app/query-report/Marketplace%20Needs%20Attention?${to_query_string({ platform })}`;
	}
	return needs_attention_url(account);
}

// Product Sync Errors covers sync_status=Error OR inventory_sync_status=Error
// on Marketplace Product Mapping - a List View URL can only AND its query
// params, so this link deliberately only scopes to the account and leaves the
// OR condition for the merchant to look at on the (already much smaller)
// filtered list, rather than guessing which of the two fields matched.
function product_sync_errors_url(account) {
	return `/app/marketplace-product-mapping?${to_query_string({ marketplace_account: account })}`;
}

function import_errors_url(account) {
	return needs_attention_url(account);
}

function to_query_string(filters) {
	return Object.entries(filters)
		.map(([fieldname, filter_value]) => {
			const raw = typeof filter_value === "string" ? filter_value : JSON.stringify(filter_value);
			return `${encodeURIComponent(fieldname)}=${encodeURIComponent(raw)}`;
		})
		.join("&");
}

if (typeof module !== "undefined") {
	module.exports = {
		MARKETPLACE_CHANNEL_STATUS_LEVEL_COLORS,
		orders_needing_attention_url,
		product_sync_errors_url,
		import_errors_url,
		to_query_string,
	};
}
