if (typeof frappe !== "undefined") {
	frappe.ui.form.on("Marketplace Product Mapping", {
		refresh(frm) {
			if (frm.is_new()) return;
			add_platform_push_button(frm);
		},
	});
}

// Fixes a known defect: this button used to show "Push to Talabat"
// unconditionally on every mapping, including Shopify ones created by #105.
// Gated client-side by looking up the linked Marketplace Account's platform
// - no schema change to Marketplace Product Mapping itself (decision 6).
function add_platform_push_button(frm) {
	// Consistent with the List View's bulk action, which gates on
	// frappe.model.can_write() before even offering the action (review
	// finding #7) - low priority today since only System Manager has any
	// permission on this doctype, but kept aligned for when that changes.
	if (!frappe.model.can_write("Marketplace Product Mapping")) return;

	return frappe.db.get_value("Marketplace Account", frm.doc.marketplace_account, "platform").then((r) => {
		const platform = r && r.message && r.message.platform;
		if (platform === "Shopify") {
			frm.add_custom_button(__("Push to Shopify"), () => push_to_shopify(frm));
		} else if (platform === "Talabat") {
			frm.add_custom_button(__("Push to Talabat"), () => push_to_talabat(frm));
		}
		// No `else` warning here (review finding #7): a lookup returning
		// nothing here means the linked Marketplace Account was deleted, or
		// the user can read this Mapping but not the Account - a
		// permission/deleted-record edge case, not "this platform doesn't
		// support a catalog push". Silently showing no push button (and no
		// message) is fine for that edge case rather than guessing at a
		// misleading explanation on every form load.
	});
}

function push_to_talabat(frm) {
	frappe
		.call({
			method: "marketplace_integrations.api.talabat_catalog.queue_mapping_catalog_sync",
			args: { mapping: frm.doc.name },
			freeze: true,
			freeze_message: __("Queuing Talabat catalog update..."),
		})
		.then(() => {
			frappe.show_alert({ message: __("Talabat catalog update queued."), indicator: "green" });
			frm.reload_doc();
		});
}

function push_to_shopify(frm) {
	frappe
		.call({
			method: "marketplace_integrations.api.shopify_catalog_push.queue_catalog_push",
			args: { mapping_names: [frm.doc.name] },
			freeze: true,
			freeze_message: __("Queuing Shopify price push..."),
		})
		.then((response) => {
			const jobs = (response.message && response.message.jobs) || [];
			const skipped_accounts = (response.message && response.message.skipped_accounts) || [];
			if (skipped_accounts.length) {
				frappe.show_alert({
					// Show the server's own reason as-is (already running OR a
					// config error) rather than assuming which one it was.
					message: frappe.utils.escape_html(skipped_accounts[0].reason),
					indicator: "orange",
				});
			}
			if (!jobs.length) return;
			return frappe
				.require("/assets/marketplace_integrations/js/shopify_catalog_push_status.js")
				.then(() => {
					new frappe.ui.ShopifyCatalogPushStatus({
						jobs,
						on_done: () => frm.reload_doc(),
					});
				});
		});
}

if (typeof module !== "undefined") {
	module.exports = { push_to_talabat, push_to_shopify, add_platform_push_button };
}
