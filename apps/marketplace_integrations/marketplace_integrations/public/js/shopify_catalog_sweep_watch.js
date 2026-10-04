// Copyright (c) 2026, Tamwenat El Zekrayat and contributors
// For license information, please see license.txt

// Shared job-status watch for a Shopify catalog sweep (#168): realtime progress
// with a polling fallback. Used by the Shopify Catalog Mapping page and the
// Marketplace Products page, so the watch logic exists in exactly one place.
// Loaded on demand via frappe.require() by each page, like remote_data_grid.js.
//
// Callers own every piece of page state and wording. `is_current()` says
// whether this watch still belongs to the page's latest sweep request; once it
// returns false the watch stops itself without calling any callback.
(function () {
	const REALTIME_EVENT = "marketplace_shopify_catalog_sweep_progress";
	const STATUS_METHOD = "marketplace_integrations.api.shopify_products.get_catalog_sweep_status";
	const POLL_INTERVAL_MS = 1500;

	frappe.provide("frappe.ui");

	// options: {job_id, is_current, on_progress(processed), on_complete(result),
	// on_failed(error), on_error()}. on_error() means the status request itself
	// failed; the watch has stopped.
	frappe.ui.watchShopifyCatalogSweep = function ({
		job_id,
		is_current,
		on_progress,
		on_complete,
		on_failed,
		on_error,
	}) {
		let timer;
		const stop = () => {
			clearInterval(timer);
			if (frappe.realtime && frappe.realtime.off) {
				frappe.realtime.off(REALTIME_EVENT, on_status);
			}
		};
		const on_status = (status) => {
			if (!status || status.job_id !== job_id) return;
			if (!is_current()) return stop();
			if (status.status === "completed") {
				stop();
				on_complete(status.result || {});
			} else if (status.status === "failed") {
				stop();
				on_failed(status.error);
			} else {
				on_progress(status.processed || 0);
			}
		};
		const check_status = () => {
			frappe
				.call({ method: STATUS_METHOD, args: { job_id } })
				.then((r) => on_status({ job_id, ...(r.message || {}) }))
				.catch(() => {
					stop();
					on_error();
				});
		};
		if (frappe.realtime && frappe.realtime.on) {
			frappe.realtime.on(REALTIME_EVENT, on_status);
		}
		timer = setInterval(check_status, POLL_INTERVAL_MS);
		check_status();
	};
})();
