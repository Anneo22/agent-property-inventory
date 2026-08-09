"""Deterministic complete pagination for inventories larger than one response."""

from __future__ import annotations

import unittest

from property_inventory.retrieval import RetrievalError, search


class FakeStore:
    def __init__(self) -> None:
        model = {
            "brand": None,
            "category": "synthetic",
            "identifiers_json": "{}",
            "interfaces_json": "[]",
            "model": None,
            "model_id": "mdl-synthetic",
            "name": "Synthetic inventory object",
            "reference_url": None,
            "specs_json": "{}",
        }
        evidence = {
            "captured_on": "2026-08-05",
            "claim_strength": "explicit_current",
            "evidence_id": "ev-synthetic",
            "evidence_type": "physical_check",
            "notes": None,
            "sensitivity": "low",
            "source_ref": "synthetic fixture",
        }
        low_items = [self.item(f"itm-{index:04d}", "low") for index in range(502)]
        canaries = [self.item("itm-0100-secret", "high"), self.item("itm-0501-secret", "high")]
        self.rows = {
            "aliases": [],
            "evidence": [evidence],
            "evidence_assets": [],
            "fact_amendments": [],
            "interfaces": [],
            "inventory_events": [],
            "item_amendments": [],
            "item_detail_amendments": [],
            "item_dimensions": [],
            "item_evidence": [
                {
                    "evidence_id": "ev-synthetic",
                    "item_id": item["item_id"],
                    "role": "primary",
                }
                for item in (*low_items, *canaries)
            ],
            "item_tags": [],
            "items": [*low_items, *canaries],
            "kit_requirements": [],
            "kit_reviews": [],
            "kits": [],
            "locations": [],
            "media_assets": [],
            "model_interfaces": [],
            "models": [model],
            "relationships": [],
            "torque_paths": [],
        }

    @staticmethod
    def item(item_id: str, sensitivity: str) -> dict:
        return {
            "acquired_on": None,
            "condition": None,
            "container_id": None,
            "identity_sensitivity": sensitivity,
            "item_id": item_id,
            "location_id": None,
            "model_id": "mdl-synthetic",
            "notes": None,
            "ownership_state": "confirmed",
            "primary_evidence_id": "ev-synthetic",
            "purchase_currency": None,
            "purchase_price": None,
            "quantity": 1,
            "receipt_ref": None,
            "replacement_value": None,
            "sensitivity": sensitivity,
            "serial_or_lot": None,
            "unit": "item",
            "value_currency": None,
            "verified_on": "2026-08-05",
        }

    def get(self, table: str, record_id: str) -> dict:
        key = {"models": "model_id"}[table]
        return next(row for row in self.rows[table] if row[key] == record_id)


class PaginationV6Test(unittest.TestCase):
    def test_every_visible_item_is_retrievable_without_hidden_canaries(self) -> None:
        store = FakeStore()
        filters = {
            "alias_kind": None,
            "category": None,
            "condition": None,
            "interface_direction": None,
            "interface_family": None,
            "interface_standard": None,
            "interface_variant": None,
            "location": None,
            "location_known": None,
            "ownership_state": None,
            "tag": None,
        }
        first = search(
            store,
            query=(),
            scope="public",
            limit=500,
            filters=filters,
        )
        self.assertEqual(first["count"], 502)
        self.assertEqual(first["page_count"], 500)
        self.assertTrue(first["truncated"])
        self.assertIsNotNone(first["next_cursor"])
        second = search(
            store,
            query=(),
            scope="public",
            limit=500,
            filters=filters,
            cursor=first["next_cursor"],
        )
        self.assertEqual(second["page_count"], 2)
        self.assertFalse(second["truncated"])
        identifiers = [
            match["item"]["item_id"]
            for page in (first, second)
            for match in page["matches"]
        ]
        self.assertEqual(len(identifiers), len(set(identifiers)))
        self.assertEqual(len(identifiers), 502)
        self.assertNotIn("secret", " ".join(identifiers))

        store.rows["items"].append(store.item("itm-9999", "low"))
        with self.assertRaisesRegex(RetrievalError, "does not match"):
            search(
                store,
                query=(),
                scope="public",
                limit=500,
                filters=filters,
                cursor=first["next_cursor"],
            )


if __name__ == "__main__":
    unittest.main()
