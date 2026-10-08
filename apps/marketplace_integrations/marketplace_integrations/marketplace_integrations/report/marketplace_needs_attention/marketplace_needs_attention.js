// Copyright (c) 2026, Tamwenat El Zekrayat and contributors
// For license information, please see license.txt

// #169 Marketplace Needs Attention - one row per problem, with the problem's
// next action rendered as a link. Every dynamic value interpolated into HTML
// or a URL here goes through escape_html()/encodeURIComponent() first, per
// docs/frappe-conventions.md's client-script escaping rule (explanation text
// comes from platform errors).

const MARKETPLACE_ATTENTION_PROBLEM_TYPES = [
	"Sync Error",
	"Modified After Sync",
	"Unmapped Items",
	"Order Import Error",
	"Fulfillment Error",
	"Webhook Retries Exhausted",
	"Noon Fulfillment",
	"Noon Return Unlinked",
];

if (typeof frappe !== "undefined") {
	frappe.query_reports["Marketplace Needs Attention"] = {
		filters: [
			{
				fieldname: "platform",
				label: __("Platform"),
				fieldtype: "Select",
				options: ["", "Talabat", "Shopify", "Noon"],
			},
			{
				fieldname: "account",
				label: __("Account"),
				fieldtype: "Link",
				options: "Marketplace Account",
			},
			{
				fieldname: "problem_type",
				label: __("Problem"),
				fieldtype: "Select",
				options: ["", ...MARKETPLACE_ATTENTION_PROBLEM_TYPES],
			},
		],
		onload(report) {
			// Namespaced + off() first: onload can run again for the same page
			// wrapper, and handlers must not stack (one click = one retry).
			report.page.wrapper
				.off("click.attention")
				.on("click.attention", "[data-attention-retry]", function (event) {
					event.preventDefault();
					retry_order_sync($(this).attr("data-attention-retry"), report);
				});
		},
		formatter(value, row, column, data, default_formatter) {
			if (column.fieldname === "explanation") {
				return frappe.utils.escape_html(data.explanation || "");
			}
			if (column.fieldname === "next_action") {
				return next_action_html(data);
			}
			return default_formatter(value, row, column, data);
		},
	};
}

function to_slug(doctype) {
	return String(doctype).toLowerCase().replace(/ /g, "-");
}

// "Retry" is a click handler (data attribute), every other action a plain
// link. "Map Item" opens a new Marketplace Product Mapping with the account
// and the first unmapped reference prefilled - unless a disabled mapping for
// it already exists (a new one would fail uniqueness), in which case the
// existing mapping opens so it can be enabled.
function next_action_html(data) {
	const label = frappe.utils.escape_html(__(data.next_action || ""));
	if (data.next_action === "Retry") {
		return `<a href="#" data-attention-retry="${frappe.utils.escape_html(data.reference_name)}">${label}</a>`;
	}
	let href;
	if (data.next_action === "Map Item" && data.next_action_mapping) {
		href = `/app/marketplace-product-mapping/${encodeURIComponent(data.next_action_mapping)}`;
	} else if (data.next_action === "Map Item") {
		const query = [["marketplace_account", data.account]];
		if (data.next_action_reference) query.push(["external_reference", data.next_action_reference]);
		href = `/app/marketplace-product-mapping/new?${query
			.map(([key, value]) => `${key}=${encodeURIComponent(value)}`)
			.join("&")}`;
	} else {
		href = `/app/${to_slug(data.reference_doctype)}/${encodeURIComponent(data.reference_name)}`;
	}
	return `<a href="${frappe.utils.escape_html(href)}">${label}</a>`;
}

// Existing explicit sync endpoint. An expected blocker is returned as
// {ok: false, error} rather than raised, so check `ok` before claiming success;
// a raised failure surfaces through frappe.call's own server-message dialog.
function retry_order_sync(marketplace_order, report) {
	frappe
		.call({
			method: "marketplace_integrations.api.marketplace_orders.sync_to_erpnext",
			args: { marketplace_order },
			freeze: true,
			freeze_message: __("Retrying ERPNext sync..."),
		})
		.then((response) => {
			const result = response.message || {};
			if (result.ok === false) {
				frappe.msgprint({
					title: __("ERPNext Sync Failed"),
					indicator: "red",
					message: frappe.utils.escape_html(result.error || ""),
				});
			} else {
				frappe.show_alert({
					message: frappe.utils.escape_html(result.executed_message || __("ERPNext sync succeeded.")),
					indicator: "green",
				});
			}
			report.refresh();
		});
}

if (typeof module !== "undefined") {
	module.exports = { MARKETPLACE_ATTENTION_PROBLEM_TYPES, next_action_html, to_slug };
}
