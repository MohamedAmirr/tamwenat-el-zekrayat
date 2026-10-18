frappe.pages["marketplace-product-management"].on_page_load = function (wrapper) {
	const page = frappe.ui.make_app_page({
		parent: wrapper,
		title: __("Marketplace Product Management"),
		single_column: true,
	});

	frappe
		.require("/assets/marketplace_integrations/css/marketplace_product_management.css")
		.then(() => build_page(page));
};

function build_page(page) {
	const container = make_element("div", "marketplace-product-management-container");
	page.body.get(0).append(container);
	container.append(
		make_element(
			"p",
			"text-muted marketplace-product-management-intro",
			__("Export a workbook, edit it, upload it for review, then apply the reviewed changes.")
		)
	);

	make_import_section(container, {
		title: __("ERPNext Items"),
		description: __(
			"Manage Item master fields and one primary barcode per Item. Mapping fields are not included in this workbook."
		),
		export_label: __("Export Items"),
		import_label: __("Import Items"),
		upload_title: __("Upload Items workbook"),
		export_method: "marketplace_integrations.api.item_import.queue_items_xlsx_export",
		preview_method: "marketplace_integrations.api.item_import.preview_items_import",
		apply_method: "marketplace_integrations.api.item_import.queue_items_import_apply",
		job_status_method: "marketplace_integrations.api.item_import.get_items_import_job_status",
		identifier: (row) => row.item_code || "",
	});

	make_import_section(container, {
		title: __("Marketplace Mappings"),
		description: __(
			"Manage marketplace listings and their ERPNext Item mappings. Item master fields are not included in this workbook."
		),
		export_label: __("Export Mappings"),
		import_label: __("Import Mappings"),
		upload_title: __("Upload Marketplace Mappings workbook"),
		export_method: "marketplace_integrations.api.mapping_import.export_mappings_xlsx",
		preview_method: "marketplace_integrations.api.mapping_import.preview_mappings_import",
		apply_method: "marketplace_integrations.api.mapping_import.apply_mappings_import",
		identifier: (row) => [row.marketplace_account, row.external_reference].filter(Boolean).join(" / "),
	});
}

function make_import_section(parent, config) {
	const section = make_element("section", "marketplace-product-management-section");
	section.append(make_element("h3", "marketplace-product-management-section-title", config.title));
	section.append(make_element("p", "text-muted", config.description));

	const actions = make_element("div", "marketplace-product-management-actions");
	const export_button = make_button(config.export_label, "btn btn-default btn-sm");
	const import_button = make_button(config.import_label, "btn btn-primary btn-sm");
	actions.append(export_button, import_button);
	section.append(actions);

	const preview = make_element("div", "marketplace-product-management-preview");
	const apply_button = make_button(__("Apply reviewed changes"), "btn btn-primary btn-sm");
	apply_button.disabled = true;
	const apply_actions = make_element("div", "marketplace-product-management-apply-actions");
	apply_actions.append(apply_button);
	section.append(preview, apply_actions);
	parent.append(section);

	// A later upload invalidates every earlier preview or apply response.
	const state = { file_url: null, has_valid_preview: false, operation_id: 0 };

	export_button.addEventListener("click", () => {
		if (config.job_status_method) {
			frappe
				.call({ method: config.export_method })
				.then((response) => watch_job(response.message, config, preview))
				.catch(() => render_request_failure(preview, __("Could not start the background job.")));
			return;
		}
		open_url_post(`/api/method/${config.export_method}`, {});
	});

	import_button.addEventListener("click", () => {
		new frappe.ui.FileUploader({
			dialog_title: config.upload_title,
			allow_multiple: false,
			disable_file_browser: true,
			allow_toggle_private: false,
			make_attachments_public: false,
			restrictions: { allowed_file_types: [".xlsx"] },
			upload_notes: __("Upload one .xlsx workbook."),
			on_success: (file_doc) => preview_uploaded_file(file_doc, config, state, preview, apply_button),
		});
	});

	apply_button.addEventListener("click", () => {
		if (!state.has_valid_preview || !state.file_url) return;

		const operation_id = state.operation_id;
		const file_url = state.file_url;
		if (config.job_status_method) {
			state.has_valid_preview = false;
			apply_button.disabled = true;
			frappe
				.call({ method: config.apply_method, args: { file_url } })
				.then((response) => {
					if (!is_current_file_operation(state, operation_id, file_url)) return;
					watch_job(
						response.message,
						config,
						preview,
						() => is_current_file_operation(state, operation_id, file_url),
						() => {
							state.file_url = null;
						}
					);
				})
				.catch(() => {
					if (is_current_file_operation(state, operation_id, file_url)) {
						render_request_failure(preview, __("Applying the workbook failed."));
					}
				});
			return;
		}
		state.has_valid_preview = false;
		apply_button.disabled = true;
		frappe
			.call({
				method: config.apply_method,
				args: { file_url },
				freeze: true,
				freeze_message: __("Applying workbook changes..."),
			})
			.then((response) => {
				if (!is_current_file_operation(state, operation_id, file_url)) return;

				state.file_url = null;
				render_preview(preview, response.message, config, { applied: true });
			})
			.catch(() => {
				if (!is_current_file_operation(state, operation_id, file_url)) return;

				render_request_failure(preview, __("Applying the workbook failed."));
			});
	});
}

function watch_job(job, config, preview, is_current = () => true, on_complete = () => {}) {
	if (!job || !job.job_id) return render_request_failure(preview, __("Could not start the background job."));
	preview.replaceChildren(make_element("p", "text-muted", __("Background job queued...")));
	let timer;
	const stop = () => {
		clearInterval(timer);
		if (frappe.realtime && frappe.realtime.off) frappe.realtime.off("marketplace_item_import_progress", on_progress);
	};
	const on_progress = (status) => {
		if (!status || status.job_id !== job.job_id) return;
		if (!is_current()) return stop();
		if (status.status === "completed") {
			stop();
			on_complete();
			if (status.kind === "export" && status.result && status.result.file_url) download_file(status.result.file_url);
			else render_preview(preview, status.result, config, { applied: true });
		} else if (status.status === "failed") {
			stop();
			render_request_failure(preview, status.error || __("The background job failed."));
		} else {
			preview.replaceChildren(
				make_element("p", "text-muted", __("Background job: {0}% complete.", [status.progress || 0]))
			);
		}
	};
	const check_status = () => {
		frappe
			.call({ method: config.job_status_method, args: { job_id: job.job_id } })
			.then((response) => on_progress({ job_id: job.job_id, ...(response.message || {}) }))
			.catch(() => {
				stop();
				if (is_current()) render_request_failure(preview, __("Could not retrieve the background job status."));
			});
	};
	if (frappe.realtime && frappe.realtime.on) frappe.realtime.on("marketplace_item_import_progress", on_progress);
	timer = setInterval(check_status, 1000);
	check_status();
}

function download_file(file_url) {
	const link = document.createElement("a");
	link.href = file_url;
	link.download = "items.xlsx";
	document.body.append(link);
	link.click();
	link.remove();
}

function preview_uploaded_file(file_doc, config, state, preview, apply_button) {
	const operation_id = ++state.operation_id;
	state.has_valid_preview = false;
	apply_button.disabled = true;

	if (!file_doc || !file_doc.file_url) {
		state.file_url = null;
		render_request_failure(preview, __("The workbook upload did not return a file."));
		return;
	}

	const file_url = file_doc.file_url;
	state.file_url = file_url;
	preview.replaceChildren(make_element("p", "text-muted", __("Reviewing workbook changes...")));

	frappe
		.call({
			method: config.preview_method,
			args: { file_url },
			freeze: true,
			freeze_message: __("Reviewing workbook changes..."),
		})
		.then((response) => {
			if (!is_current_file_operation(state, operation_id, file_url)) return;

			const result = response.message;
			state.has_valid_preview = has_applyable_rows(result);
			apply_button.disabled = !state.has_valid_preview;
			render_preview(preview, result, config);
		})
		.catch(() => {
			if (!is_current_file_operation(state, operation_id, file_url)) return;

			state.file_url = null;
			state.has_valid_preview = false;
			apply_button.disabled = true;
			render_request_failure(preview, __("Reviewing the workbook failed."));
		});
}

function is_current_file_operation(state, operation_id, file_url) {
	return state.operation_id === operation_id && state.file_url === file_url;
}

function has_applyable_rows(result) {
	if (!result || !result.counts) return false;
	return count_for(result.counts, "new") + count_for(result.counts, "changed") > 0;
}

function render_request_failure(preview, message) {
	preview.replaceChildren(make_element("p", "text-danger", message));
}

function render_preview(preview, result, config, options = {}) {
	preview.replaceChildren();
	if (!result || !Array.isArray(result.rows) || !result.counts) {
		render_request_failure(preview, __("The server returned an invalid import preview."));
		return;
	}

	const summary = make_element("div", "marketplace-product-management-summary");
	summary.append(make_element("h4", null, options.applied ? __("Apply result") : __("Preview result")));
	const counts = make_element("div", "marketplace-product-management-counts");
	for (const status of ["new", "changed", "unchanged", "error"]) {
		const count = count_for(result.counts, status);
		const count_item = make_element("span", `marketplace-product-management-count ${status}`);
		count_item.append(
			make_element("strong", null, String(count)),
			document.createTextNode(` ${status_label(status)}`)
		);
		counts.append(count_item);
	}
	summary.append(counts);
	if (options.applied) {
		summary.append(
			make_element(
				"p",
				"text-success mb-0",
				__("Applied {0} row(s).", [count_for(result, "applied_count")])
			)
		);
	}
	preview.append(summary);

	for (const status of ["new", "changed", "unchanged", "error"]) {
		const rows = result.rows.filter((row) => row && row.status === status);
		preview.append(render_status_rows(status, rows, config));
	}
}

function render_status_rows(status, rows, config) {
	const section = make_element("section", "marketplace-product-management-result-section");
	section.append(make_element("h5", null, `${status_label(status)} (${rows.length})`));
	if (!rows.length) {
		section.append(make_element("p", "text-muted", __("None.")));
		return section;
	}

	const table = make_element("table", "table table-bordered table-sm marketplace-product-management-result-table");
	const header = document.createElement("thead");
	const header_row = document.createElement("tr");
	for (const label of [__("Row"), __("Identifier"), __("Details")]) {
		header_row.append(make_element("th", null, label));
	}
	header.append(header_row);
	table.append(header);

	const body = document.createElement("tbody");
	for (const row of rows) {
		const table_row = document.createElement("tr");
		table_row.append(make_element("td", null, String(row.row || "")));
		table_row.append(make_element("td", "text-break", config.identifier(row)));
		table_row.append(render_row_details(row));
		body.append(table_row);
	}
	table.append(body);
	section.append(table);
	return section;
}

function render_row_details(row) {
	const cell = document.createElement("td");
	if (row.status === "error") {
		const errors = Array.isArray(row.errors) ? row.errors : [__("Unknown row error.")];
		const list = make_element("ul", "mb-0");
		for (const error of errors) {
			list.append(make_element("li", null, String(error)));
		}
		cell.append(list);
		return cell;
	}

	if (row.status === "changed" && row.diff) {
		const list = make_element("dl", "mb-0 marketplace-product-management-diff");
		for (const [field, values] of Object.entries(row.diff)) {
			const term = make_element("dt", null, field);
			const definition = document.createElement("dd");
			definition.append(
				make_element("span", "text-muted", display_value(values && values.old)),
				make_element("span", "marketplace-product-management-arrow", " → "),
				make_element("span", null, display_value(values && values.new))
			);
			list.append(term, definition);
		}
		cell.append(list);
		return cell;
	}

	cell.textContent = row.applied ? __("Applied.") : row.status === "new" ? __("Will be created.") : __("No changes.");
	return cell;
}

function make_element(tag_name, class_name, text) {
	const element = document.createElement(tag_name);
	if (class_name) element.className = class_name;
	if (text !== undefined && text !== null) element.textContent = String(text);
	return element;
}

function make_button(label, class_name) {
	const button = make_element("button", class_name, label);
	button.type = "button";
	return button;
}

function count_for(counts, name) {
	const count = Number(counts && counts[name]);
	return Number.isFinite(count) && count >= 0 ? count : 0;
}

function status_label(status) {
	return {
		new: __("New"),
		changed: __("Changed"),
		unchanged: __("Unchanged"),
		error: __("Errors"),
	}[status];
}

function display_value(value) {
	return value === undefined || value === null || value === "" ? __("(empty)") : String(value);
}
