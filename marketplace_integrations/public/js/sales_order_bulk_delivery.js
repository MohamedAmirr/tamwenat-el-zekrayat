// Copyright (c) 2026, Tamwenat El Zekrayat and contributors
// For license information, please see license.txt

// The bulk "Delivery Note" action of the Sales Order list (docs/marketplace-integrations/delivery-note-first.md,
// "Bulk path").
//
// ERPNext's own list script adds a "Delivery Note" item to the list's Actions menu in onload
// (erpnext/selling/doctype/sales_order/sales_order_list.js, page.add_action_item(__("Delivery Note"), ...)).
// This app's list script (sales_order_list.js) runs ERPNext's onload inside run_with_replaced_action(), which
// shows OUR dialog under the label "Delivery Note" (one shipping method Select, Bosta only for now) in place of
// ERPNext's own bulk action, which is dropped (one "Delivery Note" item only). Our dialog calls
// api/bulk_delivery_notes.create_delivery_notes, which queues a background job. The job reports on the
// realtime event "bulk_delivery_note_progress": stage "creating" (documents N of M), "waiting" (K of M accepted
// by Bosta), "done" (summary, also kept as a Notification Log). The listener is registered once on every Desk
// page (this file is an app_include_js), so the progress and the summary also show after leaving the list.
// Nothing here touches core, calls Bosta, or submits a Delivery Note.
//
// Wrapped in a function so its names never leak into the shared Desk scope.
(function () {
	const METHOD = "marketplace_integrations.api.bulk_delivery_notes.create_delivery_notes";
	const EVENT = "bulk_delivery_note_progress";
	// The server enforces the cap; this only avoids a pointless round trip.
	const MAX_ORDERS = 50;
	// Shipping methods the dialog offers. Noon and Manual delivery come later.
	const SHIPPING_METHODS = ["Bosta"];
	const [WAITING, CREATING, SENT, ACCEPTED, STILL_CREATING, SKIPPED, FAILED] = [
		"waiting",
		"creating",
		"sent",
		"accepted",
		"still_creating",
		"skipped",
		"failed",
	];
	const PROBLEMS = [FAILED, SKIPPED, STILL_CREATING];

	const state = { listview: null, dialog: null, run_id: null, dismissed: null, last: null };

	function esc(value) {
		return frappe.utils.escape_html(value == null ? "" : String(value));
	}

	function form_link(doctype, name) {
		if (!name) return "";
		const href = `/app/${frappe.router.slug(doctype)}/${encodeURIComponent(name)}`;
		return `<a href="${esc(href)}"><bdi dir="ltr">${esc(name)}</bdi></a>`;
	}

	// Run ERPNext's onload while its "Delivery Note" action item is REPLACED: the item keeps the label but
	// opens our dialog, ERPNext's own callback is dropped (one "Delivery Note" item in the menu).
	// The page method is restored afterwards, so every other action item is added exactly as ERPNext adds it.
	function run_with_replaced_action(listview, run) {
		const page = listview.page;
		const own = Object.prototype.hasOwnProperty.call(page, "add_action_item");
		const original = page.add_action_item;
		const native_label = __("Delivery Note");
		page.add_action_item = function (label, click, ...rest) {
			if (label !== native_label) return original.call(this, label, click, ...rest);
			return original.call(this, label, () => open_dialog(listview), ...rest);
		};
		try {
			run();
		} finally {
			if (own) page.add_action_item = original;
			else delete page.add_action_item;
		}
	}

	function selected_orders(listview) {
		return listview.get_checked_items(true) || [];
	}

	function open_dialog(listview) {
		state.listview = listview;
		const orders = selected_orders(listview);
		if (!orders.length) {
			frappe.msgprint(__("Select at least one Sales Order."));
			return null;
		}
		if (orders.length > MAX_ORDERS) {
			frappe.msgprint(
				__("Select at most {0} Sales Orders at a time. You selected {1}.", [MAX_ORDERS, orders.length])
			);
			return null;
		}
		const dialog = new frappe.ui.Dialog({
			title: __("Create Delivery Notes"),
			fields: [
				{
					fieldtype: "Select",
					fieldname: "shipping_method",
					label: __("Shipping Method"),
					options: SHIPPING_METHODS.join("\n"),
					default: SHIPPING_METHODS[0],
					reqd: 1,
				},
				{
					fieldtype: "HTML",
					fieldname: "help",
					options: `<p class="text-muted">${esc(
						__(
							"The cash on delivery amount is filled from each order. A fully paid order gets a Sales Invoice first and COD 0. Orders that cannot be shipped are skipped and listed with the reason."
						)
					)}</p><p class="text-muted">${esc(__("Sales Orders selected: {0}", [orders.length]))}</p>`,
				},
			],
			primary_action_label: __("Create Delivery Notes"),
			primary_action: (values) => submit(dialog, orders, values.shipping_method),
		});
		dialog.show();
		return dialog;
	}

	function submit(dialog, orders, shipping_method) {
		dialog.disable_primary_action();
		return frappe
			.call({
				method: METHOD,
				type: "POST",
				args: { sales_orders: orders, shipping_method },
				freeze: true,
				freeze_message: __("Queuing..."),
			})
			.then((response) => {
				const answer = (response && response.message) || {};
				if (!answer.queued) {
					frappe.msgprint(answer.message || __("The bulk run could not be queued."));
					dialog.enable_primary_action();
					return;
				}
				dialog.hide();
				on_progress({
					run_id: answer.run_id,
					shipping_method,
					stage: "creating",
					total: orders.length,
					processed: 0,
					rows: orders.map((name) => ({ sales_order: name, status: WAITING, message: "" })),
				});
			})
			.catch(() => {
				// Frappe already showed the server's message; keep the dialog so the person can retry.
				dialog.enable_primary_action();
			});
	}

	// -- progress and summary --------------------------------------------------------------------

	function status_pill(status, method) {
		switch (status) {
			case WAITING:
				return ["grey", __("Waiting")];
			case CREATING:
				return ["blue", __("Creating documents")];
			case SENT:
				return ["orange", __("Waiting for {0}", [method])];
			case ACCEPTED:
				return ["green", __("Accepted by {0}", [method])];
			case STILL_CREATING:
				return ["orange", __("Still creating at {0}", [method])];
			case SKIPPED:
				return ["orange", __("Skipped")];
			default:
				return ["red", __("Failed")];
		}
	}

	function count(rows, status) {
		return rows.filter((row) => row.status === status).length;
	}

	function percent(data, rows) {
		if (data.stage === "done") return 100;
		const total = Math.max(data.total || rows.length, 1);
		if (data.stage === "creating") return Math.round(((data.processed || 0) / total) * 50);
		const sent = Math.max(data.sent_total || 1, 1);
		return 50 + Math.round(((data.accepted || 0) / sent) * 50);
	}

	function stage_text(data, rows, method) {
		const total = data.total || rows.length;
		if (data.stage === "creating") return __("Creating documents {0} of {1}", [data.processed || 0, total]);
		if (data.stage === "waiting")
			return __("Waiting for {0}: {1} of {2} accepted", [method, data.accepted || 0, data.sent_total || 0]);
		const summary = data.summary || {};
		return __("{0} accepted, {1} failed, {2} skipped, {3} still creating", [
			summary.accepted != null ? summary.accepted : count(rows, ACCEPTED),
			summary.failed != null ? summary.failed : count(rows, FAILED),
			summary.skipped != null ? summary.skipped : count(rows, SKIPPED),
			summary.still_creating != null ? summary.still_creating : count(rows, STILL_CREATING),
		]);
	}

	function render_progress(data) {
		data = data || {};
		const rows = data.rows || [];
		const method = data.shipping_method || SHIPPING_METHODS[0];
		const done = data.stage === "done";
		const table_rows = rows
			.map((row) => {
				const [color, text] = status_pill(row.status, method);
				const documents = [form_link("Delivery Note", row.delivery_note), form_link("Sales Invoice", row.sales_invoice)]
					.filter(Boolean)
					.join("<br>");
				return `<tr>
					<td>${form_link("Sales Order", row.sales_order)}</td>
					<td><span class="indicator-pill ${color}">${esc(text)}</span></td>
					<td>${esc(row.message)}</td>
					<td>${documents}</td>
				</tr>`;
			})
			.join("");
		const table = rows.length
			? `<table class="table table-bordered">
				<thead><tr>
					<th>${esc(__("Sales Order"))}</th><th>${esc(__("Outcome"))}</th>
					<th>${esc(__("Details"))}</th><th>${esc(__("Documents"))}</th>
				</tr></thead>
				<tbody>${table_rows}</tbody>
			</table>`
			: `<p class="text-muted">${esc(__("No orders were processed."))}</p>`;
		const percentage = Math.max(0, Math.min(100, percent(data, rows)));
		const bar = `<div class="progress" role="progressbar" aria-valuenow="${percentage}" aria-valuemin="0" aria-valuemax="100">
			<div class="progress-bar" style="width: ${percentage}%"></div>
		</div>`;
		const footer =
			done && rows.some((row) => PROBLEMS.includes(row.status))
				? `<p class="text-muted">${esc(
						__("A summary was also saved in your notifications. Check unfinished orders in the Delivery Note list.")
				  )} <a href="/app/delivery-note">${esc(__("Delivery Note"))}</a></p>`
				: "";
		return `<p><b>${esc(stage_text(data, rows, method))}</b></p>${bar}<div class="mt-3">${table}</div>${footer}`;
	}

	// The failed, skipped and unfinished orders as plain text lines, for the clipboard.
	function problem_lines(rows) {
		return (rows || [])
			.filter((row) => PROBLEMS.includes(row.status))
			.map((row) => {
				const documents = [
					row.delivery_note ? `${__("Delivery Note")} ${row.delivery_note}` : "",
					row.sales_invoice ? `${__("Sales Invoice")} ${row.sales_invoice}` : "",
				]
					.filter(Boolean)
					.join(", ");
				return `${row.sales_order}: ${row.message}${documents ? ` (${documents})` : ""}`;
			})
			.join("\n");
	}

	function copy_problems(rows) {
		const text = problem_lines(rows);
		if (!text) return;
		frappe.utils.copy_to_clipboard(text);
		frappe.show_alert({ message: __("Copied"), indicator: "green" }, 3);
	}

	function open_progress(data) {
		const dialog = new frappe.ui.Dialog({
			title: __("Delivery Notes"),
			size: "large",
			fields: [{ fieldtype: "HTML", fieldname: "progress" }],
			primary_action_label: __("Close"),
			primary_action: () => dialog.hide(),
			on_hide: () => {
				if (state.dialog === dialog) {
					state.dialog = null;
					state.dismissed = state.run_id;
				}
			},
		});
		state.dialog = dialog;
		dialog.show();
		return dialog;
	}

	// One handler for every stage. A run the person closed stays closed until its summary arrives.
	function on_progress(data) {
		if (!data) return null;
		if (state.run_id !== data.run_id) {
			state.run_id = data.run_id;
			state.dismissed = null;
			// A new run in a reused dialog starts without the previous run's Copy button.
			const button = state.dialog && state.dialog.get_secondary_btn && state.dialog.get_secondary_btn();
			if (button && typeof button.hide === "function") button.hide();
		}
		state.last = data;
		if (!state.dialog) {
			if (state.dismissed === data.run_id && data.stage !== "done") return null;
			open_progress(data);
		}
		const dialog = state.dialog;
		dialog.fields_dict.progress.$wrapper.html(render_progress(data));
		if (data.stage === "done") {
			if (data.rows.some((row) => PROBLEMS.includes(row.status))) {
				dialog.set_secondary_action_label(__("Copy failed orders"));
				dialog.set_secondary_action(() => copy_problems(data.rows));
				const button = dialog.get_secondary_btn && dialog.get_secondary_btn();
				if (button && typeof button.show === "function") button.show();
			}
			if (state.listview && typeof state.listview.refresh === "function") state.listview.refresh();
		}
		return dialog;
	}

	// One listener for the whole session, registered once at startup on every Desk page. frappe.realtime.on
	// does not throw without a socket, it just registers nothing, so the socket is checked first when Frappe
	// exposes it; the list's onload calls this again as a second chance.
	function listen() {
		const shared = (frappe.marketplace_integrations = frappe.marketplace_integrations || {});
		if (shared.__bulk_delivery_listening) return false;
		if (!frappe.realtime || typeof frappe.realtime.on !== "function") return false;
		if ("socket" in frappe.realtime && !frappe.realtime.socket) return false;
		frappe.realtime.on(EVENT, on_progress);
		shared.__bulk_delivery_listening = true;
		return true;
	}

	const api = {
		listen,
		open_dialog,
		problem_lines,
		render_progress,
		run_with_replaced_action,
		on_progress,
		submit,
		state,
	};
	if (typeof frappe !== "undefined") {
		frappe.marketplace_integrations = frappe.marketplace_integrations || {};
		frappe.marketplace_integrations.bulk_delivery = api;
		if (typeof $ !== "undefined" && typeof document !== "undefined") {
			$(document).on("startup", () => listen());
		}
	}
	if (typeof module !== "undefined") module.exports = api;
})();
