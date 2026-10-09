const TALABAT_OUTLET_CLOSE_REASONS = [
	"TOO_BUSY_NO_DRIVERS",
	"TOO_BUSY_KITCHEN",
	"UPDATES_IN_MENU",
	"TECHNICAL_PROBLEM",
	"CLOSED",
	"OTHER",
	"BAD_WEATHER",
	"HOLIDAY_SPECIAL_DAY",
];

const TALABAT_OUTLET_FIELDS = [
	"outlet_status_section",
	"external_status",
	"external_status_reason",
	"external_status_until",
	"external_status_checked_on",
	"external_status_error",
];

const RECONCILE_INVENTORY_LABEL = "Reconcile Shopify Inventory";

const SHOPIFY_LOCATION_FIELDS = ["shopify_location_section", "shopify_location_gid", "shopify_location_name"];
const NOON_FIELDS = ["noon_warehouse_section", "noon_warehouse_code", "noon_country_code"];
const NOON_SYNC_LABEL = "Sync Noon Pricing and Stock";
const NOON_IMPORT_ORDERS_LABEL = "Import Noon Orders";
const NOON_LOOKUP_RETURN_LABEL = "Look Up Noon Return";

// Every custom button configure_outlet_management() can ever add - needed so
// a platform change away from Talabat can remove all of them (#103 review
// finding #3, mirroring finding #9's fix for the Shopify Location button).
const TALABAT_OUTLET_BUTTON_LABELS = ["Refresh Status", "Open Outlet", "Close Today", "Close Until"];

// Must stay in sync with PLATFORM_CONNECTION_DOCTYPES in marketplace_account.py.
// Duplicated here (rather than round-tripping to the server) because the
// `connection` Dynamic Link field needs `connection_doctype` filled in on a
// brand-new, unsaved in-memory doc, before any server call ever happens -
// the Python-side default in MarketplaceAccount.__init__() only takes effect
// once the document has round-tripped to the server (e.g. after a save).
const PLATFORM_CONNECTION_DOCTYPES = {
	Talabat: "Talabat Connection",
	Shopify: "Shopify Connection",
	Noon: "Noon Connection",
};

if (typeof frappe !== "undefined") {
	frappe.ui.form.on("Marketplace Account", {
		refresh(frm) {
			configure_outlet_management(frm);
			configure_shopify_location(frm);
			configure_shopify_inventory(frm);
			configure_noon(frm);
			configure_view_shortcuts(frm);
		},
		platform(frm) {
			update_connection_doctype_on_platform_change(frm);
			configure_outlet_management(frm);
			configure_shopify_location(frm);
			configure_shopify_inventory(frm);
			configure_noon(frm);
		},
		connection(frm) {
			configure_shopify_location(frm);
			configure_noon(frm);
		},
	});
}

function set_default_connection_doctype(frm) {
	// Default only - never override a connection_doctype the document
	// already has (e.g. an existing saved document being edited), mirroring
	// set_connection_doctype()'s "already set" guard on the Python side.
	if (frm.doc.connection_doctype) return;

	const expected = PLATFORM_CONNECTION_DOCTYPES[frm.doc.platform];
	if (!expected) return;

	frm.doc.connection_doctype = expected;
	frm.refresh_field("connection");
}

function update_connection_doctype_on_platform_change(frm) {
	// #103 review finding #4: unlike set_default_connection_doctype() above
	// (pure default, blank-only), a platform field CHANGE must re-derive
	// connection_doctype from the new platform too - otherwise switching an
	// existing account's platform (e.g. Talabat -> Shopify) leaves it stuck
	// on the old, now-wrong connection_doctype with no way to fix it from
	// the form (connection_doctype is hidden/read-only). Mirrors
	// update_connection_doctype_on_platform_change() on the Python side.
	const expected = PLATFORM_CONNECTION_DOCTYPES[frm.doc.platform];
	if (!expected) {
		set_default_connection_doctype(frm);
		return;
	}
	// Review finding #5: a platform change also leaves cross-platform
	// residue behind if only connection_doctype is touched - `connection`
	// still references the old platform's document (surfacing a confusing
	// generic core link error instead of a friendly one), and fields
	// specific to the platform being left are never cleared. Use
	// frm.set_value() (not a direct frm.doc.* assignment) so these are
	// properly tracked as dirty regardless of what else in this handler
	// already dirties the form.
	if (frm.doc.connection_doctype !== expected) {
		frm.set_value("connection_doctype", expected);
		frm.set_value("connection", "");
		clear_fields_for_platform_left(frm);
	}
	frm.refresh_field("connection");
}

function clear_fields_for_platform_left(frm) {
	// Clear whichever platform's fields are no longer relevant after this
	// platform change - Shopify Location fields when leaving Shopify,
	// Talabat outlet-status fields when leaving Talabat. Noon must also
	// discard any residue from either platform, including the previous
	// platform's external ID before the user enters the Noon project code.
	const fields_to_clear =
		frm.doc.platform === "Noon"
			? [
					...TALABAT_OUTLET_FIELDS,
					"external_status_payload",
					...SHOPIFY_LOCATION_FIELDS,
					...NOON_FIELDS,
					"shopify_orders_reconciled_through",
					"external_account_id",
				]
			: frm.doc.platform === "Shopify"
			? [...TALABAT_OUTLET_FIELDS, ...NOON_FIELDS]
			: [...SHOPIFY_LOCATION_FIELDS, ...NOON_FIELDS];
	for (const fieldname of fields_to_clear) {
		const field = frm.fields_dict && frm.fields_dict[fieldname];
		if (field && field.df && field.df.fieldtype === "Section Break") continue;
		frm.set_value(fieldname, null);
	}
}

function configure_outlet_management(frm) {
	const is_talabat = should_show_outlet_management(frm.doc.platform);
	for (const fieldname of TALABAT_OUTLET_FIELDS) {
		frm.toggle_display(fieldname, is_talabat);
		frm.set_df_property(fieldname, "read_only", 1);
	}
	if (!is_talabat) {
		// Review finding #3: mirror finding #9's fix for the Shopify Location
		// button - stale Talabat outlet buttons must not survive a platform
		// change away from Talabat.
		for (const label of TALABAT_OUTLET_BUTTON_LABELS) {
			frm.remove_custom_button(__(label));
		}
		return;
	}
	if (frm.is_new()) return;

	frm.add_custom_button(__("Refresh Status"), () => refresh_outlet_status(frm));
	for (const action of outlet_actions_for_status(frm.doc.external_status)) {
		add_outlet_action(frm, action);
	}
}

function should_show_outlet_management(platform) {
	return platform === "Talabat";
}

function configure_shopify_location(frm) {
	const is_shopify = should_show_shopify_location(frm.doc.platform);
	for (const fieldname of SHOPIFY_LOCATION_FIELDS) {
		frm.toggle_display(fieldname, is_shopify);
	}
	// external_account_id is auto-populated from the live-verified Shopify
	// shop GID for Shopify accounts (see validate_shopify_shop() in
	// marketplace_account.py) - never manually typed once this ships, unlike
	// Talabat's own external_account_id which stays editable exactly as
	// before.
	frm.set_df_property("external_account_id", "read_only", is_shopify ? 1 : 0);
	if (!is_shopify) {
		// Review finding #9: a stale "Load Shopify Locations" button must
		// not survive a platform change away from Shopify without a full
		// page refresh.
		frm.remove_custom_button(__("Load Shopify Locations"));
		return;
	}
	if (!frm.doc.connection) {
		// Review finding #4: the button also goes stale when platform stays
		// Shopify but connection is cleared to blank - clicking it would then
		// post an invalid (empty) connection value.
		frm.remove_custom_button(__("Load Shopify Locations"));
		return;
	}

	frm.add_custom_button(__("Load Shopify Locations"), () => show_shopify_location_picker(frm));
}

function should_show_shopify_location(platform) {
	return platform === "Shopify";
}

function configure_noon(frm) {
	const is_noon = frm.doc.platform === "Noon";
	for (const fieldname of NOON_FIELDS) frm.toggle_display(fieldname, is_noon);
	frm.remove_custom_button(__("Load Noon Warehouses"));
	frm.remove_custom_button(__(NOON_SYNC_LABEL));
	frm.remove_custom_button(__(NOON_IMPORT_ORDERS_LABEL));
	frm.remove_custom_button(__(NOON_LOOKUP_RETURN_LABEL));
	if (!is_noon) return;
	if (frm.doc.connection && frappe.model.can_write("Marketplace Account")) {
		frm.add_custom_button(__("Load Noon Warehouses"), () => show_noon_warehouse_picker(frm));
	}
	if (!frm.is_new() && frm.doc.enabled && frappe.model.can_write("Marketplace Account")) {
		frm.add_custom_button(__(NOON_SYNC_LABEL), () => sync_noon(frm));
		frm.add_custom_button(__(NOON_IMPORT_ORDERS_LABEL), () => import_noon_orders(frm));
		frm.add_custom_button(__(NOON_LOOKUP_RETURN_LABEL), () => lookup_noon_return(frm));
	}
}

// Manual barcode lookup (#153) - the manual Desk trigger for
// api/noon_returns.py::lookup_return(), same confirm/prompt -> queue ->
// "queued or already queued" toast pattern as import_noon_orders() above.
// No Noon HTTP call happens in this request; the server only enqueues a
// background worker.
function lookup_noon_return(frm) {
	frappe.prompt(
		[
			{
				fieldname: "barcode",
				fieldtype: "Data",
				label: __("Barcode"),
				reqd: 1,
			},
		],
		(values) => {
			frappe.call({
				method: "marketplace_integrations.api.noon_returns.lookup_return",
				args: { account: frm.doc.name, barcode: values.barcode },
				freeze: true,
				freeze_message: __("Queuing Noon return lookup..."),
			}).then((response) => {
				const queued = (response.message || {}).queued;
				frappe.show_alert({
					message: queued
						? __("Noon return lookup queued for barcode {0}.", [frappe.utils.escape_html(values.barcode)])
						: __("A Noon return lookup is already queued or running for barcode {0}.", [
								frappe.utils.escape_html(values.barcode),
							]),
					indicator: queued ? "green" : "orange",
				});
			});
		},
		__(NOON_LOOKUP_RETURN_LABEL),
		__("Look Up")
	);
}

// Account-level bulk order import (#145 decision 12) - the manual Desk
// trigger for api/noon_orders.py::import_orders(), which had no UI caller
// before this fix (review finding B). Matches sync_noon()'s own
// confirm -> queue -> "already queued or running" pattern exactly, since
// both enqueue on the same per-account-deduplicated `long` queue rather than
// call Noon inline.
function import_noon_orders(frm) {
	frappe.confirm(__("Import Noon orders for this account now?"), () => {
		frappe.call({
			method: "marketplace_integrations.api.noon_orders.import_orders",
			args: { account: frm.doc.name },
			freeze: true,
			freeze_message: __("Queuing Noon order import..."),
		}).then((response) => {
			const queued = (response.message || {}).queued;
			frappe.msgprint(
				queued
					? __("Noon order import queued. Refresh Marketplace Orders to see the results once it finishes.")
					: __("A Noon order import is already queued or running for this account.")
			);
		});
	});
}

function show_noon_warehouse_picker(frm) {
	frappe
		.call({
			method: "marketplace_integrations.api.noon_catalog.list_warehouses",
			args: { connection: frm.doc.connection },
			freeze: true,
			freeze_message: __("Loading Noon Warehouses..."),
		})
		.then((response) => {
			const warehouses = (response.message || []).filter((row) => row.is_active);
			if (!warehouses.length) {
				frappe.msgprint(__("No active Noon Warehouses were returned."));
				return;
			}
			const by_label = {};
			const labels = warehouses.map((row) => {
				const label = `${row.display_name} (${row.warehouse_code})`;
				by_label[label] = row.warehouse_code;
				return label;
			});
			const dialog = new frappe.ui.Dialog({
				title: __("Select Noon Warehouse"),
				fields: [{ fieldname: "warehouse", fieldtype: "Select", label: __("Warehouse"), options: labels.join("\n"), reqd: 1 }],
				primary_action_label: __("Use Warehouse"),
				primary_action(values) {
					frm.set_value("noon_warehouse_code", by_label[values.warehouse]);
					dialog.hide();
				},
			});
			dialog.show();
		});
}

function sync_noon(frm) {
	frappe.confirm(__("Push current ERPNext pricing and stock for this account to Noon?"), () => {
		frappe.call({
			method: "marketplace_integrations.api.noon_catalog.queue_account_sync",
			args: { account: frm.doc.name },
			freeze: true,
			freeze_message: __("Queuing Noon sync..."),
		}).then((response) => {
			const queued = response.message || {};
			const kinds = ["pricing", "stock"].filter((kind) => queued[kind] && queued[kind].queued);
			frappe.msgprint(
				kinds.length
					? __("Noon {0} sync queued. Mapping statuses will update when the jobs finish.", [kinds.join(" and ")])
					: __("Noon pricing and stock syncs are already queued or running.")
			);
		});
	});
}

// #107: "Reconcile Shopify Inventory" - only on a saved, enabled Shopify
// account and only for a user who can write Marketplace Accounts (the server
// re-checks write permission on the account and on its mappings). A stale
// button must not survive a platform/enabled change without a page refresh.
function configure_shopify_inventory(frm) {
	frm.remove_custom_button(__(RECONCILE_INVENTORY_LABEL));
	if (frm.is_new()) return;
	if (!should_show_shopify_location(frm.doc.platform) || !frm.doc.enabled) return;
	if (!frappe.model.can_write("Marketplace Account")) return;
	frm.add_custom_button(__(RECONCILE_INVENTORY_LABEL), () => reconcile_shopify_inventory(frm));
}

function reconcile_shopify_inventory(frm) {
	frappe.confirm(
		__(
			"Push ERPNext stock for every enabled mapping of this account to Shopify inventory? This writes to your live Shopify store."
		),
		() => {
			frappe
				.call({
					method: "marketplace_integrations.api.shopify_inventory.queue_account_reconciliation",
					args: { account: frm.doc.name },
					freeze: true,
					freeze_message: __("Queuing Shopify inventory reconciliation..."),
				})
				.then((response) => {
					const jobs = (response.message && response.message.jobs) || [];
					const skipped_accounts =
						(response.message && response.message.skipped_accounts) || [];
					if (skipped_accounts.length) {
						// The server's own reason, escaped verbatim.
						frappe.msgprint({
							title: __("Marketplace Accounts skipped"),
							message: skipped_accounts
								.map(
									(skipped) =>
										`${frappe.utils.escape_html(
											skipped.account
										)}: ${frappe.utils.escape_html(skipped.reason)}`
								)
								.join("<br>"),
							indicator: "orange",
						});
					}
					if (!jobs.length) return;
					return frappe
						.require(
							"/assets/marketplace_integrations/js/shopify_catalog_push_status.js"
						)
						.then(() => {
							new frappe.ui.ShopifyCatalogPushStatus({
								jobs,
								...frappe.ui.ShopifyCatalogPushStatus.INVENTORY_OPTIONS,
							});
						});
				});
		}
	);
}

// #167: a pure-navigation "View" button group - no new business logic, just
// routes to the existing Marketplace Order / Marketplace Product Mapping /
// Marketplace Sync Log lists, each filtered to this one Marketplace Account.
// Only shown on a saved document (frm.doc.name is what the filter needs).
function configure_view_shortcuts(frm) {
	if (frm.is_new()) return;

	frm.add_custom_button(
		__("Orders"),
		() => frappe.set_route("List", "Marketplace Order", { marketplace_account: frm.doc.name }),
		__("View")
	);
	frm.add_custom_button(
		__("Product Mappings"),
		() => frappe.set_route("List", "Marketplace Product Mapping", { marketplace_account: frm.doc.name }),
		__("View")
	);
	frm.add_custom_button(
		__("Sync Log"),
		() => frappe.set_route("List", "Marketplace Sync Log", { marketplace_account: frm.doc.name }),
		__("View")
	);
}

function show_shopify_location_picker(frm) {
	frappe
		.call({
			method: "marketplace_integrations.api.shopify_locations.list_active_locations",
			args: { connection: frm.doc.connection },
			freeze: true,
			freeze_message: __("Loading active Shopify Locations..."),
		})
		.then((r) => {
			const locations = r.message || [];
			if (!locations.length) {
				frappe.msgprint({
					title: __("Shopify Locations"),
					message: __("No active Locations were returned by Shopify."),
					indicator: "orange",
				});
				return;
			}

			const location_by_label = {};
			const labels = locations.map((location) => {
				const label = `${location.name} (${location.id})`;
				location_by_label[label] = location;
				return label;
			});

			const dialog = new frappe.ui.Dialog({
				title: __("Select Shopify Location"),
				fields: [
					{
						fieldname: "location_label",
						fieldtype: "Select",
						label: __("Active Location"),
						options: labels.join("\n"),
						reqd: 1,
					},
				],
				primary_action_label: __("Use Location"),
				primary_action(values) {
					const location = location_by_label[values.location_label];
					frm.set_value("shopify_location_gid", location.id);
					frm.set_value("shopify_location_name", location.name);
					dialog.hide();
				},
			});
			dialog.show();
		});
}

function outlet_actions_for_status(status) {
	if (status === "OPEN") return ["close_today", "close_until"];
	if (["CLOSED", "CLOSED_TODAY", "CLOSED_UNTIL"].includes(status)) {
		return ["open", "close_today", "close_until"];
	}
	return [];
}

function add_outlet_action(frm, action) {
	if (action === "open") {
		frm.add_custom_button(__("Open Outlet"), () => set_outlet_status(frm, "OPEN"));
		return;
	}
	if (action === "close_today") {
		frm.add_custom_button(__("Close Today"), () => show_close_today_dialog(frm));
		return;
	}
	frm.add_custom_button(__("Close Until"), () => show_close_until_dialog(frm));
}

function refresh_outlet_status(frm) {
	call_outlet_api(frm, "marketplace_integrations.api.talabat_outlet.refresh_outlet_status", {});
}

function set_outlet_status(frm, status, closed_reason, closed_until) {
	call_outlet_api(frm, "marketplace_integrations.api.talabat_outlet.set_outlet_status", {
		...build_outlet_status_args(status, closed_reason, closed_until),
	});
}

function build_outlet_status_args(status, closed_reason, closed_until) {
	const args = { status };
	if (closed_reason) args.closed_reason = closed_reason;
	if (closed_until) args.closed_until = closed_until;
	return args;
}

function show_close_today_dialog(frm) {
	show_close_dialog(frm, "CLOSED_TODAY", __("Close Talabat Outlet Today"));
}

function show_close_until_dialog(frm) {
	show_close_dialog(frm, "CLOSED_UNTIL", __("Close Talabat Outlet Until"), true);
}

function show_close_dialog(frm, status, title, requires_closed_until = false) {
	const fields = [
		{
			fieldname: "closed_reason",
			fieldtype: "Select",
			label: __("Reason"),
			options: TALABAT_OUTLET_CLOSE_REASONS.join("\n"),
			reqd: 1,
		},
	];
	if (requires_closed_until) {
		fields.push({
			fieldname: "closed_until",
			fieldtype: "Datetime",
			label: __("Close Until"),
			description: __("Enter your normal local date and time. Talabat receives the server-converted UTC time."),
			reqd: 1,
		});
	}
	const dialog = new frappe.ui.Dialog({
		title,
		fields,
		primary_action_label: __("Confirm"),
		primary_action(values) {
			dialog.hide();
			set_outlet_status(frm, status, values.closed_reason, values.closed_until);
		},
	});
	dialog.show();
}

function call_outlet_api(frm, method, args) {
	frappe
		.call({
			method,
			args: { account: frm.doc.name, ...args },
			freeze: true,
			freeze_message: __("Confirming outlet status with Talabat..."),
		})
		.then(() => frm.reload_doc())
		.catch(() => {
			frappe.msgprint({
				title: __("Talabat Outlet Status"),
				message: __(
					"Talabat could not confirm the outlet status. The last confirmed status remains unchanged."
				),
				indicator: "red",
			});
		});
}

if (typeof module !== "undefined") {
	module.exports = {
		TALABAT_OUTLET_CLOSE_REASONS,
		TALABAT_OUTLET_FIELDS,
		SHOPIFY_LOCATION_FIELDS,
		PLATFORM_CONNECTION_DOCTYPES,
		build_outlet_status_args,
		outlet_actions_for_status,
		should_show_outlet_management,
		should_show_shopify_location,
		set_default_connection_doctype,
		update_connection_doctype_on_platform_change,
		clear_fields_for_platform_left,
		configure_outlet_management,
		configure_shopify_location,
		configure_shopify_inventory,
		reconcile_shopify_inventory,
		RECONCILE_INVENTORY_LABEL,
		configure_view_shortcuts,
	};
}
