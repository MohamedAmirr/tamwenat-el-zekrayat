# Copyright (c) 2026, Tamwenat El Zekrayat and contributors
# For license information, please see license.txt

from frappe.model.document import Document

SANDBOX_BASE_URL = "https://sandbox.partner.deliveryhero.io"
PRODUCTION_BASE_URL = "https://talabat.partner.deliveryhero.io"


class TalabatSettings(Document):
	def get_base_url(self):
		if self.environment == "Production":
			return PRODUCTION_BASE_URL
		return SANDBOX_BASE_URL
