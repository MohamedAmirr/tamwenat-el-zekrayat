if (typeof frappe !== "undefined") {
	frappe.listview_settings["Marketplace Order"] = {
		onload(listview) {
			listview.page.add_inner_button(__("Retrieve from Talabat"), () => {
				show_talabat_retrieval_dialog(listview);
			});
			listview.page.add_inner_button(__("Retrieve from Shopify"), () => {
				show_shopify_retrieval_dialog(listview);
			});
		},
	};
}

function show_shopify_retrieval_dialog(listview) {
	const dialog = new frappe.ui.Dialog({
		title: __("Retrieve Shopify Orders"),
		fields: [
			{ fieldname: "account", fieldtype: "Link", label: __("Marketplace Account"), options: "Marketplace Account", get_query: () => ({ filters: { platform: "Shopify", enabled: 1 } }), reqd: 1 },
			{ fieldname: "start_date", fieldtype: "Date", label: __("Start Date"), reqd: 1 },
			{ fieldname: "end_date", fieldtype: "Date", label: __("End Date"), reqd: 1 },
		],
		primary_action_label: __("Retrieve"),
		primary_action(values) {
			const range = build_utc_range(values);
			if (range.error) { frappe.msgprint(range.error); return; }
			dialog.hide();
			frappe.call({
				method: "marketplace_integrations.api.shopify_orders.queue_order_import",
				args: { account: values.account, start: range.start, end: range.end },
			}).then((response) => {
				const job_id = response.message.job_id;
				frappe.show_alert({ message: __("Shopify order import queued: {0}", [job_id]), indicator: "blue" });
				// Review finding #7: get_order_import_status() was never wired to
				// any UI beyond this one-shot "queued" toast - reuse the same
				// job-status polling dialog #106/#107 already built for the
				// catalog push/inventory sync jobs, rather than a new one.
				frappe.require("/assets/marketplace_integrations/js/shopify_catalog_push_status.js").then(() => {
					new frappe.ui.ShopifyCatalogPushStatus({
						jobs: [{ job_id, account: values.account }],
						on_done: () => listview.refresh(),
						...frappe.ui.ShopifyCatalogPushStatus.ORDER_IMPORT_OPTIONS,
					});
				});
			});
		},
	});
	dialog.show();
}

function show_talabat_retrieval_dialog(listview) {
	const dialog = new frappe.ui.Dialog({
		title: __("Retrieve Talabat Orders"),
		fields: [
			{
				fieldname: "account",
				fieldtype: "Link",
				label: __("Marketplace Account"),
				options: "Marketplace Account",
				get_query: () => ({ filters: { platform: "Talabat", enabled: 1 } }),
				reqd: 1,
			},
			{
				fieldname: "start_date",
				fieldtype: "Date",
				label: __("Start Date"),
				description: __("Retrieved from the beginning of this UTC date."),
				reqd: 1,
			},
			{
				fieldname: "end_date",
				fieldtype: "Date",
				label: __("End Date"),
				description: __("Retrieved through the end of this UTC date."),
				reqd: 1,
			},
		],
		primary_action_label: __("Retrieve"),
		primary_action(values) {
			const range = build_utc_range(values);
			if (range.error) {
				frappe.msgprint(range.error);
				return;
			}
			dialog.hide();
			frappe
				.call({
					method: "marketplace_integrations.api.talabat_orders.import_vendor_orders",
					args: { account: values.account, start: range.start, end: range.end },
					freeze: true,
					freeze_message: __("Retrieving orders from Talabat..."),
				})
				.then((response) => {
					frappe.msgprint(format_import_result(response.message));
					listview.refresh();
				});
		},
	});
	dialog.show();
}

function build_utc_range(values) {
	const start_date = values.start_date;
	const end_date = values.end_date;
	if (!is_iso_date(start_date) || !is_iso_date(end_date)) {
		return { error: __("Start Date and End Date must be valid dates.") };
	}
	if (start_date > end_date) {
		return { error: __("Start Date must be before or equal to End Date.") };
	}
	return { start: `${start_date}T00:00:00Z`, end: `${end_date}T23:59:59Z` };
}

function is_iso_date(value) {
	if (!/^\d{4}-\d{2}-\d{2}$/.test(value)) {
		return false;
	}
	return new Date(`${value}T00:00:00Z`).toISOString().slice(0, 10) === value;
}

function format_import_result(result) {
	const errors = result.errors || [];
	return {
		title: __("Talabat Retrieval Complete"),
		indicator: errors.length ? "orange" : "green",
		message: errors.length
			? __("Retrieved {0} orders. {1} could not be imported.", [result.count, errors.length])
			: __("Retrieved {0} orders.", [result.count]),
	};
}

if (typeof module !== "undefined") {
	module.exports = { build_utc_range, format_import_result };
}
