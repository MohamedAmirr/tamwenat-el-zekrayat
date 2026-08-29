"""
Dev-only seed data: creates a handful of sample grocery/snack Items on the
dev site so Talabat product-mapping work has real Items to reference before
actual product data exists in ERPNext. NEVER run against a real site.

Run inside the dev bench container:

    docker exec -w /home/frappe/bench-workspace/frappe-bench tamwenat-el-zekrayat-dev-frappe-1 \\
        bash -lc "bench --site dev.localhost execute scripts.seed_dev_items.run"

(or paste the body of run() into `bench --site dev.localhost console`)
"""

import frappe

ITEM_GROUPS = ["Snacks", "Beverages"]

ITEMS = [
    {
        "item_code": "CHIPS-CLASSIC-150G",
        "item_name": "Golden Crisp Classic Salted Chips 150g",
        "item_group": "Snacks",
        "standard_rate": 35,
        "barcode": "6221031201011",
    },
    {
        "item_code": "COLA-CAN-330ML",
        "item_name": "Fizzy Cola Can 330ml",
        "item_group": "Beverages",
        "standard_rate": 15,
        "barcode": "6221031201028",
    },
    {
        "item_code": "WATER-BOTTLE-600ML",
        "item_name": "Pure Spring Water Bottle 600ml",
        "item_group": "Beverages",
        "standard_rate": 8,
        "barcode": "6221031201035",
    },
    {
        "item_code": "CHOC-BAR-MILK-45G",
        "item_name": "Milko Milk Chocolate Bar 45g",
        "item_group": "Snacks",
        "standard_rate": 20,
        "barcode": "6221031201042",
    },
    {
        "item_code": "MILK-CARTON-1L",
        "item_name": "Full Cream Milk Carton 1L",
        "item_group": "Beverages",
        "standard_rate": 45,
        "barcode": "6221031201059",
    },
]


def run():
    for group in ITEM_GROUPS:
        if not frappe.db.exists("Item Group", group):
            frappe.get_doc(
                {
                    "doctype": "Item Group",
                    "item_group_name": group,
                    "parent_item_group": "All Item Groups",
                    "is_group": 0,
                }
            ).insert()

    created = []
    for spec in ITEMS:
        if frappe.db.exists("Item", spec["item_code"]):
            continue
        doc = frappe.get_doc(
            {
                "doctype": "Item",
                "item_code": spec["item_code"],
                "item_name": spec["item_name"],
                "item_group": spec["item_group"],
                "stock_uom": "Nos",
                "standard_rate": spec["standard_rate"],
                "barcodes": [{"barcode": spec["barcode"], "barcode_type": "EAN"}],
            }
        )
        doc.insert()
        created.append(doc.item_code)

    frappe.db.commit()
    print("Created items:", created)
    print("Total Item count:", frappe.db.count("Item"))
