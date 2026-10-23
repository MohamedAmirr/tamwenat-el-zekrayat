# Copyright (c) 2026, Tamwenat El Zekrayat and contributors
# For license information, please see license.txt

"""Import the Bosta module's Sidebar on the same migrate that adds the module (#326).

``bosta/`` was added to ``modules.txt``. ``sync_module_defs`` reads ``modules.txt`` directly so
Module Def "Bosta" is created, but ``sync_for`` walks the module map cached in redis
(``app_modules``), which was built from the old ``modules.txt`` and is not rebuilt by migrate, so
``bosta/sidebar/bosta/bosta.json`` is never imported on that run. This patch refreshes the
module map and imports the file with ``force=True`` (it also moves a Sidebar "Bosta" that exists
on the old module, ``Marketplace Integrations``, to ``Bosta``). Idempotent: a forced import of the
same file twice leaves the same row.
"""

import frappe
from frappe.modules.import_file import import_file_by_path


def execute():
	frappe.cache.delete_value("app_modules")
	frappe.setup_module_map()
	import_file_by_path(
		frappe.get_app_path("marketplace_integrations", "bosta", "sidebar", "bosta", "bosta.json"),
		force=True,
	)
	frappe.clear_cache()
