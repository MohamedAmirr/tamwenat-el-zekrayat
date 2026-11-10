// Copyright (c) 2026, Tamwenat El Zekrayat and contributors
// For license information, please see license.txt

// Sales Order list extensions:
//
// - The order details hover card on every row (#325, order_hover_card.js): the helper is loaded with
//   frappe.require, the card is a convenience and failing to load it changes nothing else.
// - Two columns that say where an order is, in plain words (text plus colour, one meaning per pill):
//   * Status (replaces ERPNext's indicator for a submitted order that is not Closed / On Hold / Cancelled / Draft
//     and has no advance payment pending): Not prepared yet, Ready waiting for pickup, On the way, Delivered with
//     payment not received, Delivered and paid, Delivery problem, plus the red Waiting for Stock (#500).
//   * Payment (the placeholder Custom Field custom_payment_display): Paid in advance, Paid (invoice), Pay on delivery.
//   Both come from ONE call per list page (api/order_list_status.py): the list row alone cannot tell whether a draft
//   Delivery Note exists or whether an invoice is paid. Rows show "Loading..." until it answers; if the call fails the
//   native indicator stays and the Payment cell is empty. Every pill has a one-line hover title (meaning and next
//   step); each column header gets a "?" button that opens the legend of all states.
// - The delivery countdown column (custom_earliest_delivery_date): days left to the earliest item delivery
//   date, open marketplace orders only (format_delivery_countdown).
//
// - The bulk "Delivery Note" action of the Actions menu opens the shipping method dialog
//   (sales_order_bulk_delivery.js) in place of ERPNext's own bulk action, which is dropped.
//
// Wrapped in a function: doctype_list_js files share one scope with ERPNext's own list script.
(function () {
	const CARD_METHOD = "marketplace_integrations.api.order_hover_card.get_sales_order_card";
	const CARD_ASSET = "/assets/marketplace_integrations/js/order_hover_card.js";
	const STATES_METHOD = "marketplace_integrations.api.order_list_status.get_order_states";
	const PLATFORM_FIELD = "custom_marketplace_platform";
	const EARLIEST_FIELD = "custom_earliest_delivery_date";
	const STAGE_FIELD = "custom_order_stage";
	const PAYMENT_FIELD = "custom_payment_display";
	// Frappe's data-fieldname of the indicator column in the list header and rows.
	const STATUS_COLUMN = "status_field";
	const WAITING_FOR_STOCK = "Waiting for Stock";
	// Orders ERPNext itself marks Closed / On Hold, drafts and cancelled keep their own indicator.
	const NATIVE_ONLY_STATUSES = ["Closed", "On Hold", "Cancelled", "Draft"];
	// The countdown shows only while the order is still waiting to be delivered.
	const AWAITING_DELIVERY_STATUSES = ["To Deliver", "To Deliver and Bill"];
	const MAX_ORDERS_PER_CALL = 500;

	// name -> {stage, payment, seq}, from the server; kept between refreshes so a refresh does not flicker. `seq` is
	// the number of the request that answered: an older answer arriving after a newer one is ignored.
	const states = {};
	// name -> number of the newest request for it that failed (a failure newer than the stored state hides it).
	const failed = {};
	// name -> request number (0 = a render is about to ask): the rows that may show "Loading...". A row that is not
	// here and has no answer (a Quick List, a report) shows ERPNext's own indicator, never "Loading..." for ever.
	const expected = new Map();
	let request_seq = 0;

	// Evaluated on use (not at load) so the strings are translated once the user's language is loaded.
	function status_pills() {
		return {
			not_prepared: {
				label: __("Not prepared yet"),
				color: "gray",
				tip: __("There is no Delivery Note yet. Next step: create the Delivery Note."),
			},
			waiting_for_goods: {
				label: __("Not prepared yet"),
				color: "gray",
				tip: __("The Delivery Note is waiting for goods. Next step: receive the stock, then the note can go on."),
			},
			ready: {
				label: __("Ready, waiting for pickup"),
				color: "blue",
				tip: __("The Delivery Note is made. Next step: hand the parcel to the courier."),
			},
			on_the_way: {
				label: __("On the way"),
				color: "orange",
				tip: __("The courier has the parcel. Next step: wait for the delivery."),
			},
			delivered_unpaid: {
				label: __("Delivered, payment not received"),
				// violet, not yellow: Frappe's badge renders yellow exactly like orange (both are amber), and
				// "on the way" is orange.
				color: "violet",
				tip: __("The parcel is delivered but the payment is not in. Next step: collect the payment and make the Sales Invoice."),
			},
			delivered_paid: {
				label: __("Delivered and paid"),
				color: "green",
				tip: __("The parcel is delivered and paid. Nothing left to do."),
			},
			problem: {
				label: __("Delivery problem"),
				color: "red",
				tip: __("A parcel came back or was lost. Next step: open the Delivery Note and decide what to do."),
			},
			attempt_failed: {
				label: __("Delivery problem"),
				color: "red",
				tip: __("A delivery attempt failed. Next step: follow up with Bosta."),
			},
			waiting_for_stock: {
				label: __(WAITING_FOR_STOCK),
				color: "red",
				tip: __("There is not enough stock to reserve this order. Next step: receive stock; the order is reserved again by itself."),
			},
		};
	}

	function payment_pills() {
		return {
			advance: {
				label: __("Paid in advance"),
				color: "green",
				tip: __("The customer paid before delivery. Nothing to collect."),
			},
			invoice: {
				label: __("Paid (invoice)"),
				color: "green",
				// INTERIM (until the Bosta COD receivable "Option A" exists): an invoiced order counts as paid.
				tip: __("The order is invoiced. The cash is collected by the carrier and is settled with the carrier later."),
			},
			marketplace: {
				label: __("Paid by marketplace"),
				color: "green",
				tip: __("The marketplace collects the payment. Nothing to collect."),
			},
			cod: {
				label: __("Pay on delivery"),
				color: "gray",
				tip: __("Not paid yet. Next step: collect the payment when the parcel is delivered."),
			},
		};
	}

	// platform_badge.js is loaded before this file; looked up on use.
	function format_platform(value, df) {
		const badge = (frappe.marketplace_integrations || {}).platform_badge;
		return badge ? badge.formatter(value, df) : frappe.utils.escape_html(value || "");
	}

	// The same badge the Status column uses (Frappe's own list renders the indicator with frappe.ui.badge), so both
	// columns look alike; an older Frappe without it gets the indicator pill.
	function pill_html({ label, color, tip }) {
		if (frappe.ui && frappe.ui.badge && frappe.ui.badge.html) {
			return frappe.ui.badge.html({ label, theme: color, title: tip });
		}
		const escape = frappe.utils.escape_html;
		return `<span class="indicator-pill ${color}" title="${escape(tip)}">${escape(label)}</span>`;
	}

	function loading_pill() {
		return { label: __("Loading..."), color: "gray", tip: __("Loading...") };
	}

	// What the cache holds for an order: {state}, {failed: true} (the newest request for it failed, so an older
	// answer is not shown as current), {loading: true} (an answer is on its way) or null (nothing to show).
	function cached(name) {
		const state = states[name];
		if (failed[name] && (!state || failed[name] > state.seq)) return { failed: true };
		if (state) return { state };
		return expected.has(name) ? { loading: true } : null;
	}

	// A submitted order that the new Status column covers.
	function covered(doc) {
		return (
			!!doc &&
			doc.docstatus === 1 &&
			!NATIVE_ONLY_STATUSES.includes(doc.status) &&
			doc.advance_payment_status !== "Requested"
		);
	}

	// The Status pill of an order: {label, color, tip, filter?}, or null when ERPNext's own indicator stays.
	// Waiting for Stock (written by services/order_reservation.py) comes first; delivered splits on payment.
	function status_info(doc) {
		if (!covered(doc)) return null;
		const pills = status_pills();
		if (doc[STAGE_FIELD] === WAITING_FOR_STOCK) {
			return { ...pills.waiting_for_stock, filter: `${STAGE_FIELD},=,${WAITING_FOR_STOCK}` };
		}
		// The form header asks the same function with a full document (it has a doctype; list rows do not): the
		// list's page cache would be stale there, so the form keeps ERPNext's indicator.
		if (doc.doctype) return null;
		const entry = cached(doc.name);
		if (!entry || entry.failed) return null;
		// A real filter, like ERPNext's own pill: every pill filters the list by the order's native status.
		const filter = `status,=,${doc.status}`;
		if (entry.loading) return { ...loading_pill(), filter };
		let key = entry.state.stage;
		// delivered splits on payment: only "pay on delivery" is still unpaid (advance, invoice and marketplace are paid)
		if (key === "delivered") key = entry.state.payment === "cod" ? "delivered_unpaid" : "delivered_paid";
		return pills[key] ? { ...pills[key], filter } : null;
	}

	// The Payment column: a pill for every submitted order; empty while nothing is expected, or after a failure.
	function format_payment(value, df, doc) {
		if (!doc || doc.docstatus !== 1) return "";
		const entry = cached(doc.name);
		if (!entry || entry.failed) return "";
		if (entry.loading) return pill_html(loading_pill());
		const pill = payment_pills()[entry.state.payment];
		return pill ? pill_html(pill) : "";
	}

	// The delivery countdown column: custom_earliest_delivery_date is the earliest item delivery date, set by the
	// sync (ERPNext's own header delivery_date is the LATEST item date). Only an open marketplace order shows a
	// pill; the whole-day difference comes from frappe.datetime, never from browser timezone math.
	function format_delivery_countdown(value, df, doc) {
		if (
			!value ||
			!doc ||
			!doc.custom_marketplace_platform ||
			doc.docstatus !== 1 ||
			!AWAITING_DELIVERY_STATUSES.includes(doc.status)
		)
			return "";
		const days = frappe.datetime.get_diff(value, frappe.datetime.get_today());
		if (!Number.isFinite(days)) return "";
		let text;
		let color;
		if (days >= 2) [text, color] = [__("In {0} days", [days]), "green"];
		else if (days === 1) [text, color] = [__("Tomorrow"), "orange"];
		else if (days === 0) [text, color] = [__("Today"), "orange"];
		else if (days === -1) [text, color] = [__("Overdue 1 day"), "red"];
		else [text, color] = [__("Overdue {0} days", [-days]), "red"];
		const title = frappe.utils.escape_html(frappe.datetime.str_to_user(value));
		return `<span class="indicator-pill ${color}" title="${title}">${frappe.utils.escape_html(text)}</span>`;
	}

	// One call for the page: the stage and payment of every submitted order in it. A re-render (not a refetch)
	// follows, so the "Loading..." pills turn into the real ones.
	function page_names(listview) {
		return ((listview && listview.data) || [])
			.filter((doc) => doc.docstatus === 1)
			.map((doc) => doc.name)
			.slice(0, MAX_ORDERS_PER_CALL);
	}

	// Called before every render of the list (settings.before_render): the rows of this page may show "Loading..."
	// until load_states answers. Rows nobody asked about (Quick List, other views) never do.
	function mark_expected(listview) {
		page_names(listview).forEach((name) => {
			if (!expected.has(name)) expected.set(name, 0);
		});
	}

	function load_states(listview) {
		const names = page_names(listview);
		if (!names.length) return Promise.resolve();
		const seq = ++request_seq;
		names.forEach((name) => expected.set(name, seq));
		return frappe
			.call({ method: STATES_METHOD, args: { names: JSON.stringify(names) }, silent: true })
			.then((response) => {
				const answer = (response && response.message) || {};
				names.forEach((name) => {
					// an older answer that arrives after a newer one never overwrites it
					if (answer[name] && (!states[name] || states[name].seq < seq)) states[name] = { ...answer[name], seq };
					if (failed[name] && failed[name] < seq) delete failed[name];
				});
			})
			.catch((error) => {
				names.forEach((name) => {
					if (!states[name] || states[name].seq < seq) failed[name] = Math.max(failed[name] || 0, seq);
				});
				console.error("The Sales Order list could not load the delivery and payment states.", error);
			})
			.then(() => {
				names.forEach((name) => {
					if (expected.get(name) === seq) expected.delete(name);
				});
				if (listview && typeof listview.render === "function") listview.render();
				decorate(listview);
			});
	}

	function legend_html(kind) {
		const escape = frappe.utils.escape_html;
		const pills = kind === "payment" ? payment_pills() : status_pills();
		const rows = Object.values(pills)
			.map(
				(pill) =>
					`<div style="display:flex;flex-wrap:wrap;gap:8px;align-items:baseline;margin-block-end:8px">` +
					`${pill_html(pill)}<span>${escape(pill.tip)}</span></div>`
			)
			.join("");
		const note =
			kind === "payment"
				? ""
				: `<p class="text-muted">${escape(__("An order with several Delivery Notes shows the one that is least advanced."))}</p>`;
		return rows + note;
	}

	function show_legend(kind) {
		frappe.msgprint({
			title: kind === "payment" ? __("Payment states") : __("Status states"),
			message: legend_html(kind),
		});
	}

	// The hover title of a Status pill is Frappe's fixed "Document has been submitted"; replace it with the
	// meaning and next step. The "?" buttons open the legend of a column. Both are idempotent: they run after every
	// refresh and re-render.
	function decorate(listview) {
		if (!listview || !listview.$result || typeof listview.$result.find !== "function") return;
		const by_name = {};
		(listview.data || []).forEach((doc) => (by_name[doc.name] = doc));
		listview.$result.find(".list-row-container").each(function () {
			const $row = $(this);
			const doc = by_name[$row.find(".list-row-checkbox").attr("data-name")];
			const info = doc && status_info(doc);
			if (!info) return;
			const $cell = $row.find(`.list-row-col[data-fieldname="${STATUS_COLUMN}"]`);
			$cell.find("[title]").attr("title", info.tip);
			$cell.children().first().attr("title", info.tip);
		});
		const $head = listview.$result.find(".list-row-head");
		[
			[STATUS_COLUMN, "status"],
			[PAYMENT_FIELD, "payment"],
		].forEach(([fieldname, kind]) => {
			const $col = $head.find(`.list-row-col[data-fieldname="${fieldname}"]`);
			if (!$col.length) return;
			// The Payment column is computed per page and holds no stored value, so sorting by it means nothing.
			if (kind === "payment") $col.find("[data-sort-by]").removeAttr("data-sort-by").removeAttr("title");
			if ($col.find(".mi-legend-button").length) return;
			const label = frappe.utils.escape_html(__("What do these states mean?"));
			const $button = $(
				`<button type="button" class="btn btn-link btn-xs mi-legend-button" aria-label="${label}" title="${label}" ` +
					`style="padding-inline:4px;min-width:24px;min-height:24px">?</button>`
			);
			$button.on("click", (event) => {
				event.stopPropagation();
				show_legend(kind);
			});
			const $handle = $col.find(".list-col-resize-handle");
			if ($handle.length) $handle.before($button);
			else $col.append($button);
		});
	}

	function add_hover_card(listview) {
		return frappe
			.require(CARD_ASSET)
			.then(() => {
				frappe.marketplace_integrations.order_hover_card
					.create({ method: CARD_METHOD, arg: "sales_order" })
					.attach_to_list(listview);
			})
			.catch((error) => console.error("Order hover card failed to load (#325).", error));
	}

	// Add to ERPNext's own Sales Order list settings instead of replacing them.
	if (typeof frappe !== "undefined" && frappe.listview_settings) {
		const settings = (frappe.listview_settings["Sales Order"] = frappe.listview_settings["Sales Order"] || {});
		settings.add_fields = [
			...new Set([
				...(settings.add_fields || []),
				PLATFORM_FIELD,
				EARLIEST_FIELD,
				STAGE_FIELD,
				"advance_payment_status",
			]),
		];
		settings.formatters = {
			...(settings.formatters || {}),
			[EARLIEST_FIELD]: format_delivery_countdown,
			[PLATFORM_FIELD]: format_platform,
			[PAYMENT_FIELD]: format_payment,
		};
		// Marks the settings as already extended, so loading this file again (route revisit, asset
		// reload) never wraps onload twice.
		if (!settings.__mi_list_extended) {
			const original_get_indicator = settings.get_indicator;
			settings.get_indicator = function (doc) {
				const info = status_info(doc);
				if (info) return [info.label, info.color, info.filter];
				return original_get_indicator ? original_get_indicator.call(this, doc) : undefined;
			};
			const original_refresh = settings.refresh;
			settings.refresh = function (listview) {
				if (original_refresh) original_refresh.call(this, listview);
				decorate(listview);
				return load_states(listview);
			};
			// Frappe calls before_render (no argument) right before it renders the rows of a fetch, and
			// refresh right after: between them the rows may show "Loading...".
			let current_view = null;
			const original_before_render = settings.before_render;
			settings.before_render = function () {
				if (original_before_render) original_before_render.apply(this, arguments);
				mark_expected(current_view);
			};
			const original_onload = settings.onload;
			settings.onload = function (listview) {
				current_view = listview;
				const run_original = () => original_onload && original_onload.call(this, listview);
				// ERPNext's onload adds its own bulk "Delivery Note" action; the item with that label opens our
				// dialog instead and ERPNext's callback is dropped (sales_order_bulk_delivery.js, an app include).
				// Without it, ERPNext's stays as it is.
				const bulk = (frappe.marketplace_integrations || {}).bulk_delivery;
				if (bulk) {
					bulk.listen();
					bulk.run_with_replaced_action(listview, run_original);
				} else {
					run_original();
				}
				add_hover_card(listview);
			};
			settings.__mi_list_extended = true;
		}
	}

	if (typeof module !== "undefined") {
		module.exports = {
			add_hover_card,
			decorate,
			format_delivery_countdown,
			format_payment,
			format_platform,
			legend_html,
			load_states,
			mark_expected,
			reset_states: () => {
				Object.keys(states).forEach((name) => delete states[name]);
				Object.keys(failed).forEach((name) => delete failed[name]);
				expected.clear();
				request_seq = 0;
			},
			status_info,
		};
	}
})();
