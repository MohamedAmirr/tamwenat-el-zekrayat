// Noon Return list (#550, epic #551): "New Return" replaces the standard "Add Noon Return" action. It opens
// one dialog that looks a returned parcel's barcode up on Noon (queued, then polled), lists the return items
// found, and applies the merchant's per-item outcome (Restock / Damaged / Damaged, Noon paid) in one Confirm.
//
// Escaping contract with api/noon_returns.py: the stored lookup `error` and each apply_outcomes row
// `message` are ALREADY HTML-escaped by the server (never escape them again); `noon_return`, `outcome` and
// every Noon-supplied row field are NOT, and are escaped here at the point of interpolation.
//
// List scripts share one global scope: every top-level name here carries a noon_return_list / NOON_RETURN_LIST
// prefix so it cannot collide with another list script.

const NOON_RETURN_LIST_POLL_INTERVAL_MS = 2000;
const NOON_RETURN_LIST_POLL_TIMEOUT_MS = 60000;
const NOON_RETURN_LIST_API = "marketplace_integrations.api.noon_returns";

if (typeof frappe !== "undefined") {
	frappe.listview_settings["Noon Return"] = {
		onload(listview) {
			noon_return_list_replace_add_action(listview);
		},
	};
}

// Stored value "Sellable" is shown as "Restock". Keep in step with INSPECTION_OUTCOMES in noon_return.py.
function noon_return_list_outcome_options() {
	return [
		{ value: "Sellable", label: __("Restock") },
		{ value: "Damaged", label: __("Damaged") },
		{ value: "Damaged, Noon paid", label: __("Damaged, Noon paid") },
	];
}

function noon_return_list_outcome_label(outcome) {
	const found = noon_return_list_outcome_options().find((option) => option.value === outcome);
	return found ? found.label : outcome;
}

// The user needs to write both what the lookup is scoped to (the Marketplace Account) and what the confirm
// changes (the Noon Return); the server checks both again.
function noon_return_list_can_start() {
	return Boolean(
		frappe.model.can_write("Marketplace Account") &&
			frappe.model.can_write("Noon Return") &&
			!(frappe.boot && frappe.boot.read_only)
	);
}

// Frappe (16.52, list_view.js) draws the "Add" button in set_primary_action(), which also runs again when
// rows are checked and unchecked, and reads `settings.primary_action` for Ctrl+B and the empty-state Create
// button. So onload overrides the instance method (every later call draws "New Return") and sets
// `settings.primary_action`. Without both permissions the standard Add is left alone.
function noon_return_list_replace_add_action(listview) {
	if (!noon_return_list_can_start()) return;
	const open = () => noon_return_list_open_dialog(listview);
	listview.settings.primary_action = open;
	listview.set_primary_action = () => {
		listview.page.set_primary_action(__("New Return"), open, "plus");
	};
	listview.set_primary_action();
}

// The lookup arguments; a blank merchant code sends no `merchant_code` at all (Noon then scopes to the
// connection's default project code).
function noon_return_list_lookup_args(account, barcode, merchant_code) {
	const args = { account, barcode: String(barcode || "").trim() };
	const code = String(merchant_code || "").trim();
	if (code) args.merchant_code = code;
	return args;
}

// Polls get_lookup_result until it is done or error, the timeout passes, or the lookup is superseded
// (`is_current()` false: a newer lookup started or the dialog closed). Resolves with the result, or
// {state: "timeout"} / {state: "superseded"}. A failed poll request rejects.
function noon_return_list_poll(
	account,
	lookup_key,
	is_current,
	{ interval = NOON_RETURN_LIST_POLL_INTERVAL_MS, timeout = NOON_RETURN_LIST_POLL_TIMEOUT_MS } = {}
) {
	const started = Date.now();
	return new Promise((resolve, reject) => {
		const tick = () => {
			if (!is_current()) return resolve({ state: "superseded" });
			frappe
				.call({
					method: `${NOON_RETURN_LIST_API}.get_lookup_result`,
					args: { account, lookup_key },
					silent: true,
				})
				.then((response) => {
					if (!is_current()) return resolve({ state: "superseded" });
					const result = (response && response.message) || {};
					if (result.state === "done" || result.state === "error") return resolve(result);
					if (Date.now() - started >= timeout) return resolve({ state: "timeout" });
					setTimeout(tick, interval);
				}, reject);
		};
		setTimeout(tick, interval);
	});
}

// A row offers a choice only when it is linked to a Marketplace Order and has no outcome yet.
function noon_return_list_row_kind(row) {
	if (row.inspection_outcome) return "recorded";
	if (!row.marketplace_order) return "unlinked";
	return "choice";
}

function noon_return_list_esc(value) {
	return frappe.utils.escape_html(value == null ? "" : String(value));
}

function noon_return_list_row_html(row, index) {
	const kind = noon_return_list_row_kind(row);
	const field = (label, value_html) =>
		`<div><span class="text-muted">${noon_return_list_esc(label)}:</span> ${value_html}</div>`;
	const parts = [
		field(__("Purchase Item Nr"), `<bdi dir="ltr">${noon_return_list_esc(row.purchase_item_nr)}</bdi>`),
		field(__("Partner SKU"), `<bdi dir="ltr">${noon_return_list_esc(row.partner_sku)}</bdi>`),
		field(__("Order Nr"), `<bdi dir="ltr">${noon_return_list_esc(row.order_nr)}</bdi>`),
	];
	if (row.marketplace_order) {
		parts.push(
			field(__("Marketplace Order"), frappe.utils.get_form_link("Marketplace Order", row.marketplace_order, true))
		);
	}
	if (kind === "recorded") {
		parts.push(
			`<div class="text-muted">${noon_return_list_esc(
				__("Already recorded: {0}", [noon_return_list_outcome_label(row.inspection_outcome)])
			)}</div>`
		);
	} else if (kind === "unlinked") {
		parts.push(`<div class="text-muted">${noon_return_list_esc(__("Not linked to a Marketplace Order"))}</div>`);
	} else {
		const options = [{ value: "", label: "" }, ...noon_return_list_outcome_options()]
			.map((option) => `<option value="${noon_return_list_esc(option.value)}">${noon_return_list_esc(option.label)}</option>`)
			.join("");
		const aria = noon_return_list_esc(__("Outcome for {0}", [row.purchase_item_nr || row.name]));
		parts.push(
			`<div><select class="form-control input-sm" data-row-index="${index}" aria-label="${aria}">${options}</select></div>`
		);
	}
	return `<div class="noon-return-list-row margin-bottom" style="padding-bottom: var(--padding-sm); border-bottom: 1px solid var(--border-color);">${parts.join("")}</div>`;
}

function noon_return_list_rows_html(rows) {
	return rows.map((row, index) => noon_return_list_row_html(row, index)).join("");
}

// {noon_return, outcome} for every row the user gave a choice; a blank choice is skipped.
function noon_return_list_collect(rows, choices) {
	const allowed = new Set(noon_return_list_outcome_options().map((option) => option.value));
	return rows
		.map((row, index) => ({ row, outcome: choices[index] }))
		.filter(({ row, outcome }) => noon_return_list_row_kind(row) === "choice" && allowed.has(outcome))
		.map(({ row, outcome }) => ({ noon_return: row.name, outcome }));
}

// The persistent live region: the status field renders it once and only its content changes, so screen
// readers announce each new state.
const NOON_RETURN_LIST_STATUS_REGION = '<div data-role="status" role="status" aria-live="polite"></div>';

// A failed request is worth a Retry unless the server refused it for good: a 4xx (not 408/429) is a
// deterministic refusal (validation, permission, not found); no status, 5xx, 408 and 429 may pass.
function noon_return_list_is_transient(error) {
	const status = Number(error && error.status);
	if (!status) return true;
	return !(status >= 400 && status < 500 && status !== 408 && status !== 429);
}

// The server's message of a failed frappe.call, ESCAPED (rejection text is not pre-escaped): the
// `_server_messages` JSON is parsed twice and tags are stripped, then the text is escaped again.
function noon_return_list_error_text(error) {
	try {
		const raw = error && error.responseJSON && error.responseJSON._server_messages;
		if (raw) {
			const texts = JSON.parse(raw)
				.map((entry) => {
					const parsed = typeof entry === "string" ? JSON.parse(entry) : entry;
					return String((parsed && parsed.message) || "").replace(/<[^>]*>/g, "");
				})
				.filter(Boolean);
			if (texts.length) return noon_return_list_esc(texts.join(" "));
		}
	} catch (e) {
		// Unreadable server text: fall back to the generic message.
	}
	return "";
}

// Status content (the inner HTML of the live region). `detail.error` is HTML-safe already (server text or
// noon_return_list_error_text); `detail.retry === false` hides Retry for a deterministic refusal.
function noon_return_list_status_html(state, detail = {}) {
	if (state === "loading") {
		return `<span class="text-muted">${noon_return_list_esc(__("Looking up on Noon..."))}</span>`;
	}
	if (state === "empty") {
		return (
			`<div>${noon_return_list_esc(__("Noon returned no return items for this barcode."))}</div>` +
			`<div class="text-muted">${noon_return_list_esc(__("Try another merchant code"))}</div>`
		);
	}
	if (state === "error") {
		const text = detail.error || noon_return_list_esc(__("The lookup failed."));
		const retry =
			detail.retry === false
				? ""
				: `<button type="button" class="btn btn-xs btn-default" data-action="retry">${noon_return_list_esc(__("Retry"))}</button>`;
		return `<div class="text-danger">${text}</div>${retry}`;
	}
	if (state === "timeout") {
		return `<div class="text-warning">${noon_return_list_esc(__("Still looking up. Check the Noon Return list shortly."))}</div>`;
	}
	return "";
}

// The summary shown after Confirm. `message` is pre-escaped by the server; names and outcomes are escaped here.
function noon_return_list_summary(results) {
	const applied = results.filter((result) => result.ok);
	const refused = results.filter((result) => !result.ok);
	const items = results.map((result) => {
		const link = frappe.utils.get_form_link("Noon Return", result.noon_return, true);
		if (!result.ok) {
			return `<li>${__("{0} refused: {1}", [link, result.message || noon_return_list_esc(__("The row was refused."))])}</li>`;
		}
		const outcome = noon_return_list_esc(noon_return_list_outcome_label(result.outcome));
		let line = __("{0} recorded as {1}.", [link, outcome]);
		if (result.sales_return) {
			line += " " + __("Draft Sales Return {0}", [frappe.utils.get_form_link("Delivery Note", result.sales_return, true)]);
		}
		return `<li>${line}</li>`;
	});
	return {
		title: __("New Return"),
		indicator: refused.length ? "orange" : "green",
		message:
			`<p>${noon_return_list_esc(__("Applied: {0}", [applied.length]))}</p>` +
			`<p>${noon_return_list_esc(__("Refused: {0}", [refused.length]))}</p>` +
			`<ul>${items.join("")}</ul>` +
			`<p class="text-muted">${noon_return_list_esc(__("Create the Credit Note from each Noon Return form."))}</p>`,
	};
}

// One account is preselected (and its select hidden); with several the select starts blank so the user picks.
function noon_return_list_account_options(accounts) {
	const options = accounts.map((name) => ({ value: name, label: noon_return_list_esc(name) }));
	return accounts.length > 1 ? [{ value: "", label: "" }, ...options] : options;
}

function noon_return_list_merchant_code_options(codes) {
	return [{ value: "", label: "" }, ...(codes || []).map((code) => ({ value: code, label: noon_return_list_esc(code) }))];
}

function noon_return_list_open_dialog(listview) {
	return frappe.db
		.get_list("Marketplace Account", {
			filters: { platform: "Noon", enabled: 1 },
			fields: ["name"],
			limit: 100,
		})
		.then((rows) => {
			const accounts = (rows || []).map((row) => row.name);
			if (!accounts.length) {
				frappe.msgprint({
					title: __("New Return"),
					message: __("There is no enabled Noon Marketplace Account that you can read."),
					indicator: "orange",
				});
				return;
			}
			return noon_return_list_build_dialog(listview, accounts);
		});
}

function noon_return_list_build_dialog(listview, accounts) {
	let token = 0; // bumped by every new lookup and when the dialog closes: older polls stop answering.
	let rows = [];
	let choices = {};
	let looked_up_barcode = ""; // the barcode the rows on screen belong to.
	let confirm_enabled = false; // mirrors the primary button: a disabled button must never apply.

	// no_submit_on_enter: Frappe otherwise binds Enter on every input and select to the primary action
	// (jQuery runs that click handler even on a disabled button). Enter on the barcode is Look Up, below.
	// no_focus: the barcode is focused on page show, since the account select is hidden with one account.
	const dialog = new frappe.ui.Dialog({
		title: __("New Return"),
		no_submit_on_enter: true,
		no_focus: true,
		on_page_show: () => {
			const barcode = dialog.fields_dict.barcode;
			if (barcode && barcode.$input) barcode.$input.focus();
		},
		fields: [
			{
				fieldname: "account",
				fieldtype: "Select",
				label: __("Marketplace Account"),
				options: noon_return_list_account_options(accounts),
				default: accounts.length === 1 ? accounts[0] : "",
				hidden: accounts.length <= 1 ? 1 : 0,
				reqd: 1,
				onchange: () => on_account_change(),
			},
			{
				fieldname: "merchant_code",
				fieldtype: "Select",
				label: __("Merchant Code"),
				options: noon_return_list_merchant_code_options([]),
				description: __("Leave blank to use the connection's default project code."),
			},
			// Not reqd: Frappe's get_values() would show a Missing Fields popup on Confirm after the barcode is
			// cleared; look_up() validates it instead.
			{ fieldname: "barcode", fieldtype: "Data", label: __("Barcode") },
			{ fieldname: "status", fieldtype: "HTML", options: NOON_RETURN_LIST_STATUS_REGION },
			{ fieldname: "results", fieldtype: "HTML" },
		],
		primary_action_label: __("Confirm"),
		primary_action: () => confirm(),
		on_hide: () => {
			token += 1;
		},
	});
	dialog.add_custom_action(__("Look Up"), () => look_up());

	function set_status(state, detail) {
		dialog.fields_dict.status.$wrapper.find("[data-role='status']").html(noon_return_list_status_html(state, detail));
	}

	function render_rows() {
		dialog.fields_dict.results.$wrapper.html(noon_return_list_rows_html(rows));
		update_confirm_state();
	}

	function set_confirm(enabled) {
		confirm_enabled = enabled;
		if (enabled) dialog.enable_primary_action();
		else dialog.disable_primary_action();
	}

	function update_confirm_state() {
		set_confirm(noon_return_list_collect(rows, choices).length > 0);
	}

	function clear_results() {
		token += 1;
		rows = [];
		choices = {};
		looked_up_barcode = "";
		set_status("");
		render_rows();
	}

	function load_merchant_codes() {
		const account = dialog.get_value("account");
		dialog.set_df_property("merchant_code", "options", noon_return_list_merchant_code_options([]));
		dialog.set_value("merchant_code", "");
		if (!account) return Promise.resolve();
		return frappe
			.call({ method: `${NOON_RETURN_LIST_API}.list_merchant_codes`, args: { account }, silent: true })
			.then((response) => {
				if (dialog.get_value("account") !== account) return;
				dialog.set_df_property(
					"merchant_code",
					"options",
					noon_return_list_merchant_code_options(response && response.message)
				);
			})
			.catch(() => {
				// The code is optional: without the list the lookup still works with a blank code.
			});
	}

	function on_account_change() {
		clear_results();
		load_merchant_codes();
	}

	function look_up() {
		const account = dialog.get_value("account");
		const barcode = String(dialog.get_value("barcode") || "").trim();
		if (!account || !barcode) {
			frappe.show_alert({ message: __("Enter a barcode."), indicator: "orange" });
			return Promise.resolve();
		}
		clear_results();
		looked_up_barcode = barcode;
		const mine = token;
		const is_current = () => token === mine;
		set_status("loading");
		return frappe
			.call({
				method: `${NOON_RETURN_LIST_API}.lookup_return`,
				args: noon_return_list_lookup_args(account, barcode, dialog.get_value("merchant_code")),
				silent: true,
			})
			.then((response) => {
				if (!is_current()) return;
				const lookup_key = response && response.message && response.message.lookup_key;
				return noon_return_list_poll(account, lookup_key, is_current);
			})
			.then((result) => {
				if (!result || result.state === "superseded" || !is_current()) return;
				if (result.state === "timeout") return set_status("timeout");
				if (result.state === "error") return set_status("error", { error: result.error });
				rows = result.rows || [];
				if (!rows.length) return set_status("empty");
				set_status("");
				render_rows();
			})
			.catch((error) => {
				if (!is_current()) return;
				set_status("error", { error: noon_return_list_error_text(error), retry: noon_return_list_is_transient(error) });
			});
	}

	function confirm() {
		if (!confirm_enabled) return Promise.resolve();
		const payload = noon_return_list_collect(rows, choices);
		if (!payload.length) return Promise.resolve();
		set_confirm(false);
		return frappe
			.call({
				method: `${NOON_RETURN_LIST_API}.apply_outcomes`,
				args: { rows: payload },
				freeze: true,
				freeze_message: __("Applying outcomes..."),
			})
			.then((response) => {
				dialog.hide();
				frappe.msgprint(noon_return_list_summary((response && response.message) || []));
				listview.refresh();
			})
			.catch(() => update_confirm_state());
	}

	dialog.show();
	set_confirm(false);
	const $wrapper = dialog.$wrapper;
	// Enter in the barcode box looks it up (a scanner types the barcode and then Enter). Stopping it here keeps it
	// from reaching the Confirm button.
	$wrapper.on("keydown", "input[data-fieldname='barcode']", (event) => {
		if (event.key !== "Enter") return;
		event.preventDefault();
		event.stopPropagation();
		look_up();
	});
	// Editing the barcode after a lookup makes the rows on screen stale: clear them so they cannot be confirmed.
	$wrapper.on("input", "input[data-fieldname='barcode']", (event) => {
		if (String(event.target.value || "").trim() !== looked_up_barcode) clear_results();
	});
	$wrapper.on("change", "select[data-row-index]", (event) => {
		choices[Number(event.target.getAttribute("data-row-index"))] = event.target.value;
		update_confirm_state();
	});
	$wrapper.on("click", "[data-action='retry']", () => look_up());
	if (accounts.length === 1) load_merchant_codes();
	return dialog;
}

if (typeof module !== "undefined") {
	module.exports = {
		NOON_RETURN_LIST_POLL_INTERVAL_MS,
		NOON_RETURN_LIST_POLL_TIMEOUT_MS,
		noon_return_list_outcome_options,
		noon_return_list_can_start,
		noon_return_list_replace_add_action,
		noon_return_list_lookup_args,
		noon_return_list_poll,
		noon_return_list_row_kind,
		noon_return_list_rows_html,
		noon_return_list_collect,
		noon_return_list_status_html,
		noon_return_list_error_text,
		noon_return_list_is_transient,
		noon_return_list_summary,
		noon_return_list_open_dialog,
		noon_return_list_build_dialog,
	};
}
