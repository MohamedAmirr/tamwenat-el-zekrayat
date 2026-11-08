// Copyright (c) 2026, Tamwenat El Zekrayat and contributors
// For license information, please see license.txt

// #169 Marketplace Needs Attention - one row per problem, with the problem's
// next action rendered as a link. Every dynamic value interpolated into HTML
// or a URL here goes through escape_html()/encodeURIComponent() first, per
// docs/frappe-conventions.md's client-script escaping rule (explanation text
// comes from platform errors).

const MARKETPLACE_ATTENTION_PROBLEM_TYPES = [
	"Sync Error",
	"Needs Action",
	"Sync Pending Too Long",
	"Unmapped Items",
	"Order Import Error",
	"Fulfillment Error",
	"Webhook Retries Exhausted",
	"Noon Fulfillment",
	"Noon Return Unlinked",
	"Carrier Creation Problem",
	"Carrier Exception",
	"Carrier Return",
	"Carrier Status Stale",
	"Carrier Cancelled",
	"Carrier Return Review",
	"Picked Up Not Submitted",
	"Bosta COD Differs",
	"Order Lifecycle Mismatch",
	"Payment Error",
	"Payment Excess",
	"Payment Entry Cancelled",
];

if (typeof frappe !== "undefined") {
	frappe.query_reports["Marketplace Needs Attention"] = {
		filters: [
			{
				fieldname: "platform",
				label: __("Platform"),
				fieldtype: "Select",
				options: ["", "Talabat", "Shopify", "Noon", "Bosta"],
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
				})
				.off("click.attention-submit")
				.on("click.attention-submit", "[data-attention-retry-submit]", function (event) {
					event.preventDefault();
					retry_pickup_submit($(this).attr("data-attention-retry-submit"), report);
				})
				.off("click.attention-cod")
				.on("click.attention-cod", "[data-attention-retry-cod]", function (event) {
					event.preventDefault();
					retry_cod_update($(this).attr("data-attention-retry-cod"), report);
				});
		},
		formatter(value, row, column, data, default_formatter) {
			if (column.fieldname === "explanation") {
				return frappe.utils.escape_html(data.explanation || "");
			}
			if (column.fieldname === "next_action") {
				return next_action_html(data);
			}
			// Problem type and reference type are fixed English vocabularies: translate for display only.
			if (
				column.fieldname === "problem_type" ||
				column.fieldname === "reference_doctype" ||
				column.fieldname === "action_state" ||
				column.fieldname === "linked_doctype"
			) {
				return default_formatter(__(value || ""), row, column, data);
			}
			if (column.fieldname === "reference_name") return reference_html(data);
			if (column.fieldname === "linked_document") return linked_document_html(data);
			return default_formatter(value, row, column, data);
		},
	};
}

// #324: Dynamic Link columns get no link title from Frappe, so the server sends
// `reference_label`. The docname stays in the tooltip; both are LTR-isolated for RTL.
function reference_html(data) {
	const name = data.reference_name || "";
	if (!name) return "";
	const label = frappe.utils.escape_html(data.reference_label || name);
	const href = `/app/${to_slug(data.reference_doctype)}/${encodeURIComponent(name)}`;
	return `<a href="${frappe.utils.escape_html(href)}" title="${frappe.utils.escape_html(name)}"><bdi dir="ltr">${label}</bdi></a>`;
}

// #364: the blocking or prepared document of a Needs Action row (a Delivery Note, Noon Return, ...).
function linked_document_html(data) {
	const name = data.linked_document || "";
	if (!name || !data.linked_doctype) return "";
	const href = `/app/${to_slug(data.linked_doctype)}/${encodeURIComponent(name)}`;
	const label = frappe.utils.escape_html(name);
	return `<a href="${frappe.utils.escape_html(href)}"><bdi dir="ltr">${label}</bdi></a>`;
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
	if (data.next_action === "Retry" && data.problem_type === "Bosta COD Differs") {
		// #501: send the Delivery Note's COD to Bosta again
		return `<a href="#" data-attention-retry-cod="${frappe.utils.escape_html(data.reference_name)}">${label}</a>`;
	}
	if (data.next_action === "Retry" && data.reference_doctype === "Delivery Note") {
		// #500: the pickup submit of a Created draft, not an order sync
		return `<a href="#" data-attention-retry-submit="${frappe.utils.escape_html(data.reference_name)}">${label}</a>`;
	}
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

// #500: queues the submit of a Created draft Delivery Note whose carrier picked it up. The job runs in the
// background, so the row stays until the note is submitted; the alert says it was queued, not done.
function retry_pickup_submit(delivery_note, report) {
	frappe
		.call({
			method: "marketplace_integrations.api.carrier_shipments.retry_pickup_submit",
			type: "POST",
			args: { delivery_note },
			freeze: true,
			freeze_message: __("Queueing the Delivery Note submit..."),
		})
		.then(() => {
			frappe.show_alert({
				message: __("The Delivery Note submit was queued. Refresh in a moment."),
				indicator: "green",
			});
			report.refresh();
		});
}

// #501: sends the Delivery Note's COD to Bosta again. A refusal is raised by the server and shown by Frappe; the
// row stays until Bosta holds the note's COD.
function retry_cod_update(delivery_note, report) {
	frappe
		.call({
			method: "marketplace_integrations.api.bosta_shipments.retry_cod_update",
			type: "POST",
			args: { delivery_note },
			freeze: true,
			freeze_message: __("Updating the COD at Bosta..."),
		})
		.then(() => {
			frappe.show_alert({ message: __("Bosta has the COD of the Delivery Note."), indicator: "green" });
			report.refresh();
		});
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
	module.exports = {
		MARKETPLACE_ATTENTION_PROBLEM_TYPES,
		linked_document_html,
		next_action_html,
		reference_html,
		to_slug,
	};
}
