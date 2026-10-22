// Copyright (c) 2026, Tamwenat El Zekrayat and contributors
// For license information, please see license.txt

// Sales Order list: "Create Bosta Shipments" bulk action (#305).
//
// The server checks every selected order without calling Bosta (get_bulk_create_plan) and sorts
// it into skipped / sent without review / needs review. Orders that need review open the
// existing Create Bosta Shipment dialog (from sales_order.js) one at a time. Nothing is sent
// until the review is finished; closing a dialog cancels the whole run. Then one background job
// creates the shipments and the shared status dialog shows Created / Failed / Skipped per order.
//
// Also adds the order details hover card to every row (#325, order_hover_card.js): the helper is
// loaded with frappe.require, the card is a convenience and failing to load it changes nothing else.
//
// Wrapped in a function: doctype_list_js files share one scope with ERPNext's own list script.
(function () {
	const PLAN_METHOD = "marketplace_integrations.api.bosta_shipments.get_bulk_create_plan";
	const DEFAULTS_METHOD = "marketplace_integrations.api.bosta_shipments.get_create_defaults";
	const QUEUE_METHOD = "marketplace_integrations.api.bosta_shipments.queue_bulk_create";
	const FORM_ASSET = "/assets/marketplace_integrations/js/sales_order.js";
	const STATUS_ASSET = "/assets/marketplace_integrations/js/shopify_catalog_push_status.js";
	const CARD_METHOD = "marketplace_integrations.api.order_hover_card.get_sales_order_card";
	const CARD_ASSET = "/assets/marketplace_integrations/js/order_hover_card.js";
	const MAX_ORDERS = 50;

	// The creator roles come from sales_order.js, so they are known once it has loaded.
	function add_bulk_action(listview) {
		return frappe.require(FORM_ASSET).then(() => {
			const roles = frappe.marketplace_integrations.BOSTA_CREATOR_ROLES;
			if (!(frappe.user && frappe.user.has_role(roles))) return;
			listview.page.add_actions_menu_item(__("Create Bosta Shipments"), () => start(listview));
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

	function start(listview) {
		const names = listview.get_checked_items(true);
		if (!names.length) return;
		if (names.length > MAX_ORDERS) {
			frappe.msgprint(
				__("You can create at most {0} Bosta shipments at once. You selected {1}.", [MAX_ORDERS, names.length])
			);
			return;
		}
		return frappe
			.require([FORM_ASSET, STATUS_ASSET])
			.then(() =>
				frappe.call({
					method: PLAN_METHOD,
					type: "POST",
					args: { sales_orders: names },
					freeze: true,
					freeze_message: __("Checking the selected Sales Orders..."),
				})
			)
			.then((r) => confirm_plan(listview, r.message || {}));
	}

	// Lists the pre-check skips with their reasons before anything is sent.
	function confirm_plan(listview, plan) {
		const ready = plan.ready || [];
		const review = plan.review || [];
		const skipped = plan.skipped || [];
		const escape = frappe.utils.escape_html;
		let html = `<p>${escape(__("{0} will be sent without review.", [ready.length]))}</p>`;
		html += `<p>${escape(__("{0} need you to check the details one by one.", [review.length]))}</p>`;
		if (skipped.length) {
			html += `<p>${escape(__("{0} will be skipped:", [skipped.length]))}</p><ul>`;
			skipped.forEach((row) => {
				html += `<li>${escape(row.sales_order)}: ${escape(row.reason)}</li>`;
			});
			html += "</ul>";
		}
		const dialog = new frappe.ui.Dialog({
			title: __("Create Bosta Shipments"),
			fields: [{ fieldtype: "HTML", fieldname: "summary", options: html }],
		});
		if (!ready.length && !review.length) {
			dialog.show();
			return;
		}
		dialog.set_primary_action(__("Continue"), () => {
			dialog.hide();
			review_and_send(listview, plan);
		});
		dialog.show();
	}

	function order_from_defaults(entry) {
		const defaults = entry.defaults;
		return {
			sales_order: entry.sales_order,
			receiver_name: defaults.receiver_name,
			receiver_phone: defaults.receiver_phone,
			city_id: defaults.city_id,
			address_first_line: defaults.address_first_line,
			expected_cod: defaults.cod_amount,
		};
	}

	// Opens the Create dialog for one order. Resolves with the order to send, `{ skip: true }`
	// or `{ cancel: true }` (the dialog was closed).
	function ask_one(entry, position, total) {
		return new Promise((resolve) => {
			let settled = false;
			const finish = (value) => {
				settled = true;
				resolve(value);
			};
			frappe.marketplace_integrations.open_bosta_create_dialog({
				defaults: entry.defaults,
				progress: `${__("Order {0} of {1}", [position, total])}: ${entry.sales_order}`,
				submit(dialog, values, city) {
					finish({
						order: {
							sales_order: entry.sales_order,
							receiver_name: values.receiver_name,
							receiver_phone: values.receiver_phone,
							city_id: city.id,
							address_first_line: values.address_first_line,
							expected_cod: entry.defaults.cod_amount,
						},
					});
					dialog.hide();
				},
				on_skip(dialog) {
					finish({ skip: true });
					dialog.hide();
				},
				on_hide() {
					if (!settled) finish({ cancel: true });
				},
			});
		});
	}

	// Asks about each entry in turn. Resolves with `{ orders, skipped }`, or null when the
	// operator closed a dialog (nothing is sent then).
	async function ask_all(entries) {
		const orders = [];
		const skipped = [];
		for (let index = 0; index < entries.length; index++) {
			const answer = await ask_one(entries[index], index + 1, entries.length);
			if (answer.cancel) return null;
			if (answer.skip) {
				skipped.push({ sales_order: entries[index].sales_order, reason: __("Skipped by you.") });
			} else {
				orders.push(answer.order);
			}
		}
		return { orders, skipped };
	}

	async function review_and_send(listview, plan) {
		const asked = await ask_all(plan.review || []);
		if (!asked) {
			frappe.show_alert({ message: __("Cancelled. Nothing was sent to Bosta."), indicator: "orange" });
			return;
		}
		const orders = (plan.ready || []).map(order_from_defaults).concat(asked.orders);
		const skipped = (plan.skipped || []).concat(asked.skipped);
		return send(listview, orders, skipped);
	}

	function send(listview, orders, skipped) {
		if (!orders.length) {
			show_skipped_only(skipped);
			return;
		}
		const used = {};
		orders.forEach((order) => {
			used[order.sales_order] = order;
		});
		return frappe
			.call({
				method: QUEUE_METHOD,
				type: "POST",
				args: { orders, skipped },
				freeze: true,
				freeze_message: __("Queuing the Bosta shipments..."),
			})
			.then((r) => {
				const job_id = r.message.job_id;
				let status;
				status = new frappe.ui.ShopifyCatalogPushStatus({
					jobs: [{ job_id, account: "" }],
					...frappe.ui.ShopifyCatalogPushStatus.BOSTA_BULK_OPTIONS,
					on_done: () => {
						listview.refresh();
						offer_retry(listview, status, job_id, used);
					},
				});
			});
	}

	function show_skipped_only(skipped) {
		const escape = frappe.utils.escape_html;
		const lines = skipped.map((row) => `<li>${escape(row.sales_order)}: ${escape(row.reason)}</li>`);
		frappe.msgprint({ title: __("Create Bosta Shipments"), message: `<ul>${lines.join("")}</ul>` });
	}

	// "Retry failed" reopens the Create dialog for each failed order with the values already
	// used. The server resumes the existing Bosta Shipment, so Bosta never gets a duplicate.
	function offer_retry(listview, status, job_id, used) {
		const state = (status && status.job_states[job_id]) || {};
		const results = (state.result && state.result.results) || [];
		const failed = results.filter((row) => !row.ok && !row.skipped && used[row.mapping_name]);
		if (!failed.length) return;
		status.dialog.set_primary_action(__("Retry failed"), async () => {
			status.dialog.hide();
			const entries = [];
			for (const row of failed) entries.push(await retry_entry(used[row.mapping_name]));
			const asked = await ask_all(entries);
			if (!asked) {
				frappe.show_alert({ message: __("Cancelled. Nothing was sent to Bosta."), indicator: "orange" });
				return;
			}
			return send(listview, asked.orders, asked.skipped);
		});
	}

	// Fresh server defaults (COD) under the values the operator already used, including the
	// city. If the defaults can no longer be read, the used values stand alone.
	function retry_entry(order) {
		const used = {
			receiver_name: order.receiver_name,
			receiver_phone: order.receiver_phone,
			address_first_line: order.address_first_line,
			city_id: order.city_id,
		};
		const fallback = { sales_order: order.sales_order, defaults: { ...used, cod_amount: order.expected_cod } };
		return frappe
			.call({ method: DEFAULTS_METHOD, type: "POST", silent: true, args: { sales_order: order.sales_order } })
			.then((r) => ({ sales_order: order.sales_order, defaults: { ...(r.message || {}), ...used } }))
			.catch(() => fallback);
	}

	// Add the action to ERPNext's own Sales Order list settings instead of replacing them.
	if (typeof frappe !== "undefined" && frappe.listview_settings) {
		const settings = (frappe.listview_settings["Sales Order"] = frappe.listview_settings["Sales Order"] || {});
		if (!settings.__mi_bosta_bulk) {
			const original_onload = settings.onload;
			settings.onload = function (listview) {
				if (original_onload) original_onload.call(this, listview);
				add_bulk_action(listview);
				add_hover_card(listview);
			};
			settings.__mi_bosta_bulk = true;
		}
	}

	if (typeof module !== "undefined") {
		module.exports = { add_hover_card, add_bulk_action, start, confirm_plan, review_and_send, send, offer_retry, retry_entry };
	}
})();
