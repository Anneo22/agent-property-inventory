"""Adversarial scope tests for extended item context."""

from __future__ import annotations

import json
import unittest
from typing import Any

from property_inventory.retrieval import item_context, search


class InMemoryStore:
    def __init__(self, rows: dict[str, list[dict[str, Any]]]) -> None:
        self.rows = rows

    def get(self, table: str, identifier: str) -> dict[str, Any]:
        key = {"models": "model_id"}[table]
        return next(row for row in self.rows[table] if row[key] == identifier)


class ExtendedItemContextTest(unittest.TestCase):
    def setUp(self) -> None:
        self.target_id = "itm-target"
        self.store = InMemoryStore(
            {
                "items": [
                    self._item(self.target_id, "low"),
                    self._item("itm-low-component", "low"),
                    self._item("itm-CANARY-PERSONAL-ITEM", "personal"),
                    self._item("itm-CANARY-HIGH-ITEM", "high"),
                ],
                "models": [
                    {
                        "model_id": "model-tool",
                        "name": "Scope test tool",
                        "brand": None,
                        "model": None,
                        "category": "tool",
                        "interfaces_json": "[]",
                        "specs_json": "{}",
                        "identifiers_json": "{}",
                        "reference_url": None,
                    }
                ],
                "locations": [],
                "evidence": [
                    self._evidence("ev-public", "low"),
                    self._evidence("ev-CANARY-PERSONAL", "personal"),
                    self._evidence("ev-CANARY-HIGH", "high"),
                ],
                "item_evidence": [
                    {"item_id": self.target_id, "evidence_id": "ev-public", "role": "primary"},
                    {
                        "item_id": self.target_id,
                        "evidence_id": "ev-CANARY-PERSONAL",
                        "role": "supporting",
                    },
                    {
                        "item_id": self.target_id,
                        "evidence_id": "ev-CANARY-HIGH",
                        "role": "supporting",
                    },
                ],
                "media_assets": [
                    self._asset("asset-public", "a" * 64, "low"),
                    self._asset("asset-CANARY-PERSONAL", "b" * 64, "personal"),
                    self._asset("asset-CANARY-HIGH", "c" * 64, "high"),
                ],
                "evidence_assets": [
                    self._asset_link("ev-public", "asset-public"),
                    self._asset_link("ev-CANARY-PERSONAL", "asset-CANARY-PERSONAL"),
                    self._asset_link("ev-CANARY-HIGH", "asset-CANARY-HIGH"),
                ],
                "inventory_events": [],
                "relationships": [],
                "item_tags": [
                    {
                        "item_id": self.target_id,
                        "tag": "canary-tag",
                        "evidence_id": "ev-CANARY-HIGH",
                        "sensitivity": "high",
                        "notes": "private classification",
                    }
                ],
                "kits": [
                    self._kit("kit-target", self.target_id, "ev-public"),
                    self._kit("kit-needs-target", "itm-low-component", "ev-public"),
                    self._kit("kit-CANARY-PERSONAL", self.target_id, "ev-CANARY-PERSONAL"),
                    self._kit("kit-CANARY-HIGH", self.target_id, "ev-CANARY-HIGH"),
                    self._kit("kit-CANARY-MISSING-EVIDENCE", self.target_id, "ev-missing"),
                ],
                "kit_requirements": [
                    self._requirement(
                        "kit-target", "public-component", "itm-low-component", "ev-public"
                    ),
                    self._requirement(
                        "kit-target",
                        "CANARY-PERSONAL-COMPONENT",
                        "itm-CANARY-PERSONAL-ITEM",
                        "ev-CANARY-PERSONAL",
                    ),
                    self._requirement(
                        "kit-target",
                        "CANARY-HIGH-COMPONENT",
                        "itm-CANARY-HIGH-ITEM",
                        "ev-CANARY-HIGH",
                    ),
                    self._requirement(
                        "kit-needs-target", "target-required", self.target_id, "ev-public"
                    ),
                    self._requirement(
                        "kit-CANARY-PERSONAL",
                        "personal-required",
                        None,
                        "ev-CANARY-PERSONAL",
                    ),
                    self._requirement(
                        "kit-CANARY-HIGH", "high-required", None, "ev-CANARY-HIGH"),
                ],
                "kit_reviews": [],
                "torque_paths": [
                    self._torque("path-public", "ev-public"),
                    self._torque("path-CANARY-PERSONAL", "ev-CANARY-PERSONAL"),
                    self._torque("path-CANARY-HIGH", "ev-CANARY-HIGH"),
                    self._torque("path-CANARY-MISSING-EVIDENCE", "ev-missing"),
                ],
                "aliases": [],
                "model_interfaces": [],
                "interfaces": [],
                "item_dimensions": [],
                "item_amendments": [],
                "item_detail_amendments": [],
                "fact_amendments": [],
            }
        )

    @staticmethod
    def _item(item_id: str, sensitivity: str) -> dict[str, Any]:
        return {
            "item_id": item_id,
            "model_id": "model-tool",
            "sensitivity": sensitivity,
            "identity_sensitivity": sensitivity,
            "location_id": None,
            "container_id": None,
            "notes": None,
            "serial_or_lot": None,
            "receipt_ref": None,
            "purchase_price": None,
            "purchase_currency": None,
            "replacement_value": None,
            "value_currency": None,
            "primary_evidence_id": "ev-public",
            "ownership_state": "confirmed",
            "condition": None,
            "quantity": 1,
            "unit": "item",
        }

    @staticmethod
    def _evidence(evidence_id: str, sensitivity: str) -> dict[str, str | None]:
        return {
            "evidence_id": evidence_id,
            "evidence_type": "physical_check",
            "source_ref": f"source:{evidence_id}",
            "captured_on": "2026-08-05",
            "claim_strength": "explicit_current",
            "sensitivity": sensitivity,
            "notes": f"notes:{evidence_id}",
        }

    @staticmethod
    def _asset(asset_id: str, digest: str, sensitivity: str) -> dict[str, Any]:
        return {
            "asset_id": asset_id,
            "sha256": digest,
            "byte_size": 42,
            "media_type": "image/jpeg",
            "original_name": f"{asset_id}.jpg",
            "captured_on": "2026-08-05",
            "sensitivity": sensitivity,
            "uri": f"media://sha256/{digest}",
        }

    @staticmethod
    def _asset_link(evidence_id: str, asset_id: str) -> dict[str, str | None]:
        return {
            "evidence_id": evidence_id,
            "asset_id": asset_id,
            "role": "crop",
            "region_json": '{"x":1,"y":2,"width":3,"height":4}',
        }

    @staticmethod
    def _kit(kit_id: str, serves_item_id: str, evidence_id: str) -> dict[str, str]:
        return {
            "kit_id": kit_id,
            "name": kit_id,
            "serves_item_id": serves_item_id,
            "evidence_id": evidence_id,
            "notes": f"notes for {kit_id}",
        }

    @staticmethod
    def _requirement(
        kit_id: str, requirement_key: str, item_id: str | None, evidence_id: str
    ) -> dict[str, str | None]:
        return {
            "kit_id": kit_id,
            "requirement_key": requirement_key,
            "item_id": item_id,
            "status": "source_present" if item_id else "not_recorded",
            "evidence_id": evidence_id,
            "recorded_at": "2026-08-05T00:00:00+00:00",
            "verified_event_sequence": 1 if item_id else None,
            "notes": f"notes for {requirement_key}",
        }

    def _torque(self, path_id: str, evidence_id: str) -> dict[str, str | None]:
        return {
            "path_id": path_id,
            "tool_item_id": self.target_id,
            "output_drive": "1/4 inch",
            "min_torque_nm": None,
            "max_torque_nm": None,
            "adapter_description": None,
            "adapter_max_torque_nm": None,
            "status": "direct",
            "evidence_id": evidence_id,
            "notes": f"notes for {path_id}",
        }

    def target_context(self, scope: str) -> dict[str, Any]:
        target = next(row for row in self.store.rows["items"] if row["item_id"] == self.target_id)
        return item_context(self.store, target, scope=scope)

    def test_context_includes_only_evidence_and_endpoints_visible_at_each_scope(self) -> None:
        public = self.target_context("public")
        personal = self.target_context("personal")
        private = self.target_context("private")

        self.assertEqual(
            [row["kit_id"] for row in public["kits"]], ["kit-needs-target", "kit-target"]
        )
        self.assertEqual(
            [row["requirement_key"] for row in public["kit_requirements"]],
            ["target-required", "public-component"],
        )
        self.assertEqual([row["path_id"] for row in public["torque_paths"]], ["path-public"])
        self.assertEqual([row["item_id"] for row in public["item_tags"]], [])
        self.assertEqual(len(public["evidence"]), 1)
        self.assertIsNone(public["evidence"][0]["evidence_id"])
        self.assertIsNone(public["evidence"][0]["source_ref"])
        self.assertIsNone(public["evidence"][0]["assets"][0]["sha256"])
        self.assertIsNone(public["evidence"][0]["assets"][0]["region"])
        self.assertTrue(
            all(row["evidence_id"] is None and row["notes"] is None for row in public["kits"])
        )
        self.assertTrue(
            all(
                row["evidence_id"] is None and row["notes"] is None
                for row in public["kit_requirements"] + public["torque_paths"]
            )
        )
        self.assertNotIn("CANARY-PERSONAL", json.dumps(public, sort_keys=True))
        self.assertNotIn("CANARY-HIGH", json.dumps(public, sort_keys=True))

        self.assertEqual(
            [row["kit_id"] for row in personal["kits"]],
            ["kit-CANARY-PERSONAL", "kit-needs-target", "kit-target"],
        )
        self.assertEqual(
            [row["path_id"] for row in personal["torque_paths"]],
            ["path-CANARY-PERSONAL", "path-public"],
        )
        self.assertNotIn("CANARY-HIGH", json.dumps(personal, sort_keys=True))
        self.assertEqual(len(personal["evidence"]), 2)

        self.assertEqual(
            [row["kit_id"] for row in private["kits"]],
            ["kit-CANARY-HIGH", "kit-CANARY-PERSONAL", "kit-needs-target", "kit-target"],
        )
        self.assertEqual(
            [row["tag"] for row in private["item_tags"]], ["canary-tag"]
        )
        self.assertEqual(
            [row["path_id"] for row in private["torque_paths"]],
            ["path-CANARY-HIGH", "path-CANARY-PERSONAL", "path-public"],
        )
        self.assertEqual(len(private["evidence"]), 3)
        public_proof = next(
            row for row in private["evidence"] if row["evidence_id"] == "ev-public"
        )
        self.assertEqual(public_proof["item_role"], "primary")
        self.assertEqual(public_proof["assets"][0]["sha256"], "a" * 64)
        self.assertEqual(
            public_proof["assets"][0]["region"],
            {"x": 1, "y": 2, "width": 3, "height": 4},
        )

    def test_unsourced_tags_are_private_only_in_context_filter_and_search_corpus(self) -> None:
        filters = {
            "category": None,
            "ownership_state": None,
            "condition": None,
            "location": None,
            "tag": "CANARY-TAG",
            "alias_kind": None,
            "interface_family": None,
            "interface_standard": None,
            "interface_variant": None,
            "interface_direction": None,
            "location_known": None,
        }
        for scope in ("public", "personal"):
            with self.subTest(scope=scope):
                self.assertEqual(self.target_context(scope)["item_tags"], [])
                self.assertEqual(
                    search(
                        self.store,
                        query=["CANARY-TAG"],
                        scope=scope,
                        limit=10,
                        filters={**filters, "tag": None},
                    )["matches"],
                    [],
                )
                self.assertEqual(
                    search(
                        self.store,
                        query=["scope"],
                        scope=scope,
                        limit=10,
                        filters=filters,
                    )["matches"],
                    [],
                )

        private = search(
            self.store,
            query=["CANARY-TAG"],
            scope="private",
            limit=10,
            filters={**filters, "tag": None},
        )
        self.assertEqual([match["item"]["item_id"] for match in private["matches"]], [self.target_id])
        private_filter = search(
            self.store,
            query=["scope"],
            scope="private",
            limit=10,
            filters=filters,
        )
        self.assertEqual(
            [match["item"]["item_id"] for match in private_filter["matches"]], [self.target_id]
        )


if __name__ == "__main__":
    unittest.main()
