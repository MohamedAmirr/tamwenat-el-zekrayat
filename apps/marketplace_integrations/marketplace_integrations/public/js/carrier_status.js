// Copyright (c) 2026, Tamwenat El Zekrayat and contributors
// For license information, please see license.txt

// Shared display helper for the external carrier status (Bosta states, Noon Shipping Status values) in the
// Delivery Note and Sales Order lists: one color map, the pill markup, and the rule that turns the Sales
// Order's {status: count} summary into one label. Loaded first in `doctype_list_js` of both lists
// (hooks.py) and reached through frappe.marketplace_integrations.carrier_status.
//
// Wrapped in a function: doctype_list_js files share one scope with ERPNext's own list script.
(function () {
	const COLORS = {
		green: ["Delivered", "Fulfilled"],
		blue: [
			"Pickup requested",
			"Route Assigned",
			"Picking up",
			"Picked up",
			"Picked up from business",
			"Picking up from consignee",
			"Picked up from consignee",
			"Received at warehouse",
			"In transit between Hubs",
			"Acknowledged",
			"Shipped",
			"Created",
		],
		orange: [
			"Waiting for route",
			"On hold",
			"Returned to business",
			"Returned to stock",
			"Mixed",
			"Partially Delivered",
			"Awaiting Return",
		],
		red: ["Exception", "Lost", "Damaged", "Investigation", "Awaiting your action", "Terminated", "Out of Stock"],
		grey: ["Canceled", "Cancelled", "Archived"],
	};
	const STATUS_COLORS = {};
	Object.entries(COLORS).forEach(([color, states]) => states.forEach((state) => (STATUS_COLORS[state] = color)));

	const DELIVERED = "Delivered";
	const PARTIALLY_DELIVERED = "Partially Delivered";
	// States that need a person; they stay visible next to a partial state.
	const PROBLEM_STATES = [
		"Exception",
		"Lost",
		"Damaged",
		"Investigation",
		"Terminated",
		"Awaiting your action",
		"Out of Stock",
	];

	function color_of(status) {
		return STATUS_COLORS[status] || "grey";
	}

	function escape(value) {
		return frappe.utils.escape_html(value);
	}

	// The stored value is JSON written by the server; parse it defensively and keep only
	// {text: positive integer} entries.
	function parse_summary(value) {
		let data = value;
		if (typeof value === "string") {
			try {
				data = JSON.parse(value);
			} catch (error) {
				return [];
			}
		}
		if (!data || typeof data !== "object" || Array.isArray(data)) return [];
		return Object.entries(data).filter(([, count]) => Number.isInteger(count) && count > 0);
	}

	function pill(status, count) {
		const counted = count === undefined ? "" : ` <bdi dir="ltr">${escape(count)}</bdi>`;
		return `<span class="indicator-pill ${color_of(status)}">${escape(__(status))}${counted}</span>`;
	}

	// One status of one Delivery Note, as a pill; nothing for a note without a carrier status.
	function status_pill(status) {
		return status ? pill(status) : "";
	}

	// Decide from the notes' statuses (not quantities): one state -> "single"; some Delivered and the rest
	// not -> "partial"; no Delivered but different states -> "mixed".
	function classify(entries) {
		const problems = entries.filter(([status]) => PROBLEM_STATES.includes(status));
		let kind = "mixed";
		if (entries.length === 1) kind = "single";
		else if (entries.some(([status]) => status === DELIVERED)) kind = "partial";
		return { kind, problems };
	}

	// The Sales Order list column: single state and mixed show the counts pills, partial shows
	// "Partially Delivered" plus a red pill per problem state so it is not hidden.
	function summary_html(entries) {
		if (!entries.length) return "";
		const { kind, problems } = classify(entries);
		if (kind === "partial") {
			return [pill(PARTIALLY_DELIVERED), ...problems.map(([status, count]) => pill(status, count))].join(" ");
		}
		return entries.map(([status, count]) => pill(status, count)).join(" ");
	}

	// The Sales Order list status indicator: [label, color], or null when empty. The label is escaped here
	// for the form header (`page.set_indicator` inserts it raw); the list view escapes it again when it
	// renders the badge, which only matters for a translation containing & < > " ' ` or =.
	function indicator_for(entries) {
		if (!entries.length) return null;
		const { kind, problems } = classify(entries);
		if (kind === "single") return [escape(__(entries[0][0])), color_of(entries[0][0])];
		const counted = (list) => list.map(([status, count]) => `${escape(__(status))} ${escape(count)}`);
		if (kind === "partial") {
			if (!problems.length) return [escape(__(PARTIALLY_DELIVERED)), "orange"];
			return [[escape(__(PARTIALLY_DELIVERED)), ...counted(problems)].join(", "), "red"];
		}
		return [counted(entries).join(", "), problems.length ? "red" : "orange"];
	}

	const api = {
		STATUS_COLORS,
		PROBLEM_STATES,
		PARTIALLY_DELIVERED,
		color_of,
		parse_summary,
		pill,
		status_pill,
		classify,
		summary_html,
		indicator_for,
	};

	if (typeof frappe !== "undefined") {
		frappe.marketplace_integrations = frappe.marketplace_integrations || {};
		frappe.marketplace_integrations.carrier_status = api;
	}
	if (typeof module !== "undefined") {
		module.exports = api;
	}
})();
