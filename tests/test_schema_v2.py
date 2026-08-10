"""Focused schema-v6 rebuild and verification tests."""

from __future__ import annotations

import hashlib
import json
import sqlite3
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

HERE = Path(__file__).resolve().parents[1]
REBUILD = HERE / "src" / "property_inventory" / "rebuild.py"
RENDER = HERE / "src" / "property_inventory" / "render.py"
SCHEMA = HERE / "src" / "property_inventory" / "schema.sql"
VERIFY = HERE / "src" / "property_inventory" / "verify.py"


class SchemaV6Test(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory(prefix="inventory-schema-v2-")
        self.root = Path(self.temp.name)
        self.store = self.root / "store"
        self.store.mkdir()
        self.database = self.root / "inventory.sqlite"
        self.markdown = self.root / "Inventory.md"
        self.media = self.root / "media"

    def tearDown(self) -> None:
        self.temp.cleanup()

    def write_store(self, overrides: dict[str, list[dict]] | None = None) -> dict[str, list[dict]]:
        rows: dict[str, list[dict]] = {
            "metadata": [{"inventory_id": "inv-test", "schema_version": 7}],
            "proposal_commits": [],
            "locations": [
                {
                    "location_id": "loc-test",
                    "name": "Test location",
                    "parent_location_id": None,
                    "kind": "room",
                    "sensitivity": "personal",
                    "notes": None,
                }
            ],
            "models": [
                {
                    "model_id": "mdl-test",
                    "name": "Test connector",
                    "brand": None,
                    "model": None,
                    "category": "test",
                    "specs_json": "{}",
                    "interfaces_json": "[]",
                    "identifiers_json": "{}",
                    "reference_url": None,
                }
            ],
            "evidence": [
                {
                    "evidence_id": "ev-test",
                    "evidence_type": "physical_check",
                    "source_ref": "test fixture",
                    "captured_on": "2026-08-05",
                    "claim_strength": "explicit_current",
                    "sensitivity": "personal",
                    "notes": None,
                }
            ],
            "media_assets": [
                {
                    "asset_id": "asset-test",
                    "sha256": hashlib.sha256(b"").hexdigest(),
                    "byte_size": 0,
                    "media_type": "image/jpeg",
                    "original_name": "test.jpg",
                    "captured_on": "2026-08-05",
                    "sensitivity": "personal",
                    "uri": "media://sha256/" + hashlib.sha256(b"").hexdigest(),
                }
            ],
            "interfaces": [
                {
                    "interface_id": "if-test",
                    "family": "hex",
                    "standard": "1/4 inch",
                    "variant": None,
                    "direction": "bidirectional",
                    "properties_json": "{}",
                    "notes": None,
                }
            ],
            "items": [
                {
                    "item_id": "itm-test",
                    "model_id": "mdl-test",
                    "quantity": 1,
                    "unit": "item",
                    "ownership_state": "confirmed",
                    "location_id": "loc-test",
                    "container_id": None,
                    "home_location_id": None,
                    "home_container_id": None,
                    "condition": None,
                    "serial_or_lot": None,
                    "acquired_on": None,
                    "purchase_price": None,
                    "purchase_currency": None,
                    "replacement_value": None,
                    "value_currency": None,
                    "receipt_ref": None,
                    "verified_on": "2026-08-05",
                    "sensitivity": "personal",
                    "identity_sensitivity": "personal",
                    "primary_evidence_id": "ev-test",
                    "notes": None,
                }
            ],
            "item_evidence": [{"item_id": "itm-test", "evidence_id": "ev-test", "role": "primary"}],
            "evidence_assets": [
                {
                    "evidence_id": "ev-test",
                    "asset_id": "asset-test",
                    "role": "source",
                    "region_json": '{"page":1}',
                }
            ],
            "model_interfaces": [
                {
                    "model_id": "mdl-test",
                    "interface_id": "if-test",
                    "role": "accepts",
                    "evidence_id": "ev-test",
                    "notes": None,
                }
            ],
            "relationships": [],
            "item_documents": [],
            "torque_paths": [],
            "kits": [],
            "kit_requirements": [],
            "kit_reviews": [],
            "item_tags": [],
            "aliases": [],
            "spatial_profiles": [],
            "valuations": [],
            "capture_sessions": [],
            "capture_observations": [],
            "maintenance_sessions": [],
            "maintenance_session_items": [],
            "sync_receipts": [],
            "item_dimensions": [],
            "item_amendments": [],
            "item_detail_amendments": [],
            "fact_amendments": [],
            "parties": [],
            "item_party_relations": [],
            "location_embodiments": [],
            "inventory_events": [
                {
                    "event_id": "evt-ingested-test",
                    "sequence": 1,
                    "item_id": "itm-test",
                    "event_type": "ingested",
                    "occurred_on": "2026-08-05",
                    "observed_on": "2026-08-05",
                    "occurred_on_precision": "exact",
                    "actor": "Test",
                    "evidence_id": "ev-test",
                    "location_id": "loc-test",
                    "container_id": None,
                    "area_location_id": None,
                    "context_quality": "bound",
                    "details_json": None,
                    "notes": None,
                },
                {
                    "event_id": "evt-test",
                    "sequence": 2,
                    "item_id": "itm-test",
                    "event_type": "physically_verified",
                    "occurred_on": "2026-08-05",
                    "observed_on": "2026-08-05",
                    "occurred_on_precision": "exact",
                    "actor": "Test",
                    "evidence_id": "ev-test",
                    "location_id": "loc-test",
                    "container_id": None,
                    "area_location_id": None,
                    "context_quality": "bound",
                    "details_json": None,
                    "notes": None,
                },
            ],
        }
        rows.update(overrides or {})
        for table, table_rows in rows.items():
            (self.store / f"{table}.jsonl").write_text(
                "".join(json.dumps(row, sort_keys=True) + "\n" for row in table_rows)
            )
        return rows

    def command(self, script: Path, *arguments: str) -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            [sys.executable, str(script), *arguments],
            text=True,
            capture_output=True,
            check=False,
        )

    def rebuild(self) -> subprocess.CompletedProcess[str]:
        return self.command(
            REBUILD,
            "--store",
            str(self.store),
            "--schema",
            str(SCHEMA),
            "--database",
            str(self.database),
        )

    def test_v2_projection_verifies_with_assets_interfaces_and_sequences(self) -> None:
        self.write_store()
        digest = hashlib.sha256(b"").hexdigest()
        media_path = self.media / "sha256" / digest[:2] / digest
        media_path.parent.mkdir(parents=True)
        media_path.write_bytes(b"")
        rebuilt = self.rebuild()
        self.assertEqual(rebuilt.returncode, 0, rebuilt.stderr)
        self.assertEqual(json.loads(rebuilt.stdout)["counts"]["metadata"], 1)
        self.assertEqual(json.loads(rebuilt.stdout)["counts"]["media_assets"], 1)
        self.assertEqual(json.loads(rebuilt.stdout)["counts"]["model_interfaces"], 1)

        rendered = self.command(
            RENDER, "--database", str(self.database), "--output", str(self.markdown)
        )
        self.assertEqual(rendered.returncode, 0, rendered.stderr)
        verified = self.command(
            VERIFY,
            "--store",
            str(self.store),
            "--database",
            str(self.database),
            "--markdown",
            str(self.markdown),
            "--media-root",
            str(self.media),
        )
        self.assertEqual(verified.returncode, 0, verified.stdout + verified.stderr)
        self.assertEqual(json.loads(verified.stdout)["status"], "pass")

    def test_missing_or_future_metadata_refuses_before_replacing_database(self) -> None:
        self.database.write_bytes(b"existing projection must survive")
        self.write_store({"metadata": []})
        missing = self.rebuild()
        self.assertNotEqual(missing.returncode, 0)
        self.assertIn("metadata.jsonl must contain exactly one record", missing.stderr)
        self.assertEqual(self.database.read_bytes(), b"existing projection must survive")

        self.write_store({"metadata": [{"inventory_id": "inv-test", "schema_version": 8}]})
        future = self.rebuild()
        self.assertNotEqual(future.returncode, 0)
        self.assertIn("newer than supported schema", future.stderr)
        self.assertEqual(self.database.read_bytes(), b"existing projection must survive")

    def test_evidence_bearing_v6_structures_project_losslessly(self) -> None:
        digest = hashlib.sha256(b"").hexdigest()
        self.write_store(
            {
                "evidence": [
                    {
                        "evidence_id": "ev-test",
                        "evidence_type": "physical_check",
                        "source_ref": "test fixture",
                        "captured_on": "2026-08-05",
                        "claim_strength": "explicit_current",
                        "sensitivity": "personal",
                        "notes": None,
                    },
                    {
                        "evidence_id": "ev-receipt",
                        "evidence_type": "merchant_account",
                        "source_ref": "test receipt fixture",
                        "captured_on": "2026-08-06",
                        "claim_strength": "purchase_only",
                        "sensitivity": "personal",
                        "notes": None,
                    },
                ],
                "item_evidence": [
                    {"item_id": "itm-test", "evidence_id": "ev-test", "role": "primary"},
                    {
                        "item_id": "itm-test",
                        "evidence_id": "ev-receipt",
                        "role": "supporting",
                    },
                ],
                "locations": [
                    {
                        "location_id": "loc-test",
                        "name": "Test location",
                        "parent_location_id": None,
                        "kind": "container",
                        "sensitivity": "personal",
                        "notes": None,
                    }
                ],
                "aliases": [
                    {
                        "alias_id": "alias-test",
                        "item_id": "itm-test",
                        "alias": "test alias",
                        "alias_kind": "label",
                        "evidence_id": "ev-test",
                        "sensitivity": "personal",
                        "notes": None,
                    }
                ],
                "spatial_profiles": [
                    {
                        "profile_id": "spatial-test",
                        "location_id": "loc-test",
                        "profile_json": (
                            '{"height":20,"kind":"container_box","depth":40,'
                            '"unit":"cm","width":30,"x":0,"y":0,"z":0}'
                        ),
                        "evidence_id": "ev-test",
                        "sensitivity": "personal",
                        "notes": None,
                    }
                ],
                "valuations": [
                    {
                        "valuation_id": "valuation-test",
                        "item_id": "itm-test",
                        "amount": 25.0,
                        "currency": "GBP",
                        "valued_on": "2026-08-06",
                        "basis": "receipt",
                        "evidence_id": "ev-receipt",
                        "sensitivity": "personal",
                        "notes": None,
                    }
                ],
                "capture_sessions": [
                    {
                        "capture_session_id": "capture-test",
                        "captured_on": "2026-08-06",
                        "evidence_id": "ev-test",
                        "sensitivity": "personal",
                        "provenance_state": "legacy_unbound",
                        "artifact_sha256": None,
                        "artifact_json": None,
                        "review_sha256": None,
                        "review_json": None,
                        "notes": None,
                    }
                ],
                "capture_observations": [
                    {
                        "observation_id": "observation-test",
                        "capture_session_id": "capture-test",
                        "observation_index": 1,
                        "item_id": "itm-test",
                        "observation_json": '{"barcode":"test"}',
                        "evidence_id": "ev-test",
                        "sensitivity": "personal",
                        "validation_state": "legacy_unknown",
                        "notes": None,
                    }
                ],
                "maintenance_sessions": [
                    {
                        "maintenance_session_id": "maintenance-test",
                        "performed_on": "2026-08-06",
                        "activity": "checked",
                        "elapsed_seconds": 60,
                        "correction_count": 1,
                        "review_count": 2,
                        "evidence_id": "ev-test",
                        "sensitivity": "personal",
                        "notes": None,
                    }
                ],
                "maintenance_session_items": [
                    {"maintenance_session_id": "maintenance-test", "item_id": "itm-test"}
                ],
                "sync_receipts": [
                    {
                        "sync_receipt_id": "sync-test",
                        "replica_ref": "test replica",
                        "payload_digest": digest,
                        "recorded_at": "2026-08-06T00:00:00+00:00",
                        "evidence_id": "ev-test",
                        "sensitivity": "personal",
                        "notes": None,
                    }
                ],
            }
        )
        media_path = self.media / "sha256" / digest[:2] / digest
        media_path.parent.mkdir(parents=True)
        media_path.write_bytes(b"")
        rebuilt = self.rebuild()
        self.assertEqual(rebuilt.returncode, 0, rebuilt.stderr)
        counts = json.loads(rebuilt.stdout)["counts"]
        for table in (
            "aliases",
            "spatial_profiles",
            "valuations",
            "capture_sessions",
            "capture_observations",
            "maintenance_sessions",
            "sync_receipts",
        ):
            self.assertEqual(counts[table], 1)
        rendered = self.command(
            RENDER, "--database", str(self.database), "--output", str(self.markdown)
        )
        self.assertEqual(rendered.returncode, 0, rendered.stderr)
        verified = self.command(
            VERIFY,
            "--store",
            str(self.store),
            "--database",
            str(self.database),
            "--markdown",
            str(self.markdown),
            "--media-root",
            str(self.media),
        )
        self.assertEqual(verified.returncode, 0, verified.stdout + verified.stderr)

    def test_malformed_v6_row_refuses_before_replacing_database(self) -> None:
        self.database.write_bytes(b"existing projection must survive")
        self.write_store(
            {
                "aliases": [
                    {
                        "alias_id": "alias-test",
                        "item_id": "itm-test",
                        "alias": "test alias",
                        "alias_kind": "label",
                        "evidence_id": "ev-test",
                        "sensitivity": "personal",
                    }
                ]
            }
        )
        malformed = self.rebuild()
        self.assertNotEqual(malformed.returncode, 0)
        self.assertIn("aliases.jsonl row 1 does not match schema columns", malformed.stderr)
        self.assertEqual(self.database.read_bytes(), b"existing projection must survive")

    def test_v6_semantic_leaks_and_malformed_rows_preserve_projection(self) -> None:
        digest = hashlib.sha256(b"").hexdigest()
        rows = self.write_store()
        rows["locations"] = [
            {
                "location_id": "loc-test",
                "name": "Test location",
                "parent_location_id": None,
                "kind": "container",
                "sensitivity": "personal",
                "notes": None,
            }
        ]
        rows["aliases"] = [
            {
                "alias_id": "alias-test",
                "item_id": "itm-test",
                "alias": "test",
                "alias_kind": "label",
                "evidence_id": "ev-test",
                "sensitivity": "low",
                "notes": None,
            }
        ]
        rows["spatial_profiles"] = [
            {
                "profile_id": "spatial-test",
                "location_id": "loc-test",
                "profile_json": "[]",
                "evidence_id": "ev-test",
                "sensitivity": "low",
                "notes": None,
            }
        ]
        rows["valuations"] = [
            {
                "valuation_id": "valuation-test",
                "item_id": "itm-test",
                "amount": -1,
                "currency": "GBP",
                "valued_on": "2026-8-6",
                "basis": "receipt",
                "evidence_id": "ev-test",
                "sensitivity": "low",
                "notes": None,
            }
        ]
        rows["capture_sessions"] = [
            {
                "capture_session_id": "capture-test",
                "captured_on": "2026-8-6",
                "evidence_id": "ev-test",
                "sensitivity": "low",
                "provenance_state": "legacy_unbound",
                "artifact_sha256": None,
                "artifact_json": None,
                "review_sha256": None,
                "review_json": None,
                "notes": None,
            }
        ]
        rows["capture_observations"] = [
            {
                "observation_id": "observation-test",
                "capture_session_id": "capture-test",
                "observation_index": 1,
                "item_id": "itm-test",
                "observation_json": "[]",
                "evidence_id": "ev-test",
                "sensitivity": "low",
                "validation_state": "legacy_unknown",
                "notes": None,
            }
        ]
        rows["maintenance_sessions"] = [
            {
                "maintenance_session_id": "maintenance-test",
                "performed_on": "2026-8-6",
                "activity": "checked",
                "elapsed_seconds": True,
                "correction_count": -1,
                "review_count": 1.5,
                "evidence_id": "ev-test",
                "sensitivity": "low",
                "notes": None,
            }
        ]
        rows["maintenance_session_items"] = [
            {"maintenance_session_id": "maintenance-test", "item_id": "itm-test"}
        ]
        rows["sync_receipts"] = [
            {
                "sync_receipt_id": "sync-test",
                "replica_ref": "test",
                "payload_digest": digest,
                "recorded_at": "2026-08-06T00:00:00",
                "evidence_id": "ev-test",
                "sensitivity": "low",
                "notes": None,
            }
        ]
        for table, table_rows in rows.items():
            (self.store / f"{table}.jsonl").write_text(
                "".join(
                    json.dumps(row, allow_nan=True, sort_keys=True) + "\n" for row in table_rows
                )
            )
        self.database.write_bytes(b"existing projection must survive")
        rejected = self.rebuild()
        self.assertNotEqual(rejected.returncode, 0)
        for phrase in (
            "alias sensitivity",
            "spatial profile profile_json",
            "valuation has invalid amount",
            "capture session has non-canonical",
            "capture observation observation_json",
            "maintenance session has invalid elapsed_seconds",
            "sync receipt has non-canonical",
        ):
            self.assertIn(phrase, rejected.stderr)
        self.assertEqual(self.database.read_bytes(), b"existing projection must survive")

    def test_shared_evidence_cannot_be_less_sensitive_than_any_supported_item(self) -> None:
        rows = self.write_store()
        shared_item = dict(rows["items"][0], item_id="itm-high", sensitivity="high")
        rows["items"].append(shared_item)
        rows["item_evidence"].append(
            {"item_id": "itm-high", "evidence_id": "ev-test", "role": "supporting"}
        )
        rows["evidence"][0]["sensitivity"] = "personal"
        for table, table_rows in rows.items():
            (self.store / f"{table}.jsonl").write_text(
                "".join(json.dumps(row, sort_keys=True) + "\n" for row in table_rows)
            )
        self.database.write_bytes(b"existing projection must survive")
        rejected = self.rebuild()
        self.assertNotEqual(rejected.returncode, 0)
        self.assertIn("evidence sensitivity is lower than supported item: ev-test", rejected.stderr)
        self.assertEqual(self.database.read_bytes(), b"existing projection must survive")

    def test_spatial_profile_requires_one_strict_canonical_shape(self) -> None:
        malformed_profiles = (
            {},
            {"kind": "unknown"},
            {"kind": "floor_rectangle", "x": 0, "y": 0, "width": 1, "height": 1},
            {
                "kind": "container_box",
                "x": 0,
                "y": 0,
                "z": 0,
                "width": float("nan"),
                "height": 1,
                "depth": 1,
                "unit": "m",
            },
        )
        for index, profile in enumerate(malformed_profiles):
            with self.subTest(index=index):
                rows = self.write_store(
                    {
                        "spatial_profiles": [
                            {
                                "profile_id": "spatial-test",
                                "location_id": "loc-test",
                                "profile_json": json.dumps(profile, allow_nan=True),
                                "evidence_id": "ev-test",
                                "sensitivity": "personal",
                                "notes": None,
                            }
                        ]
                    }
                )
                rows["locations"] = [
                    {
                        "location_id": "loc-test",
                        "name": "Test location",
                        "parent_location_id": None,
                        "kind": "container",
                        "sensitivity": "personal",
                        "notes": None,
                    }
                ]
                for table, table_rows in rows.items():
                    (self.store / f"{table}.jsonl").write_text(
                        "".join(
                            json.dumps(row, allow_nan=True, sort_keys=True) + "\n"
                            for row in table_rows
                        )
                    )
                self.database.write_bytes(b"existing projection must survive")
                rejected = self.rebuild()
                self.assertNotEqual(rejected.returncode, 0)
                self.assertIn("invalid canonical shape", rejected.stderr)
                self.assertEqual(self.database.read_bytes(), b"existing projection must survive")

    def test_spatial_profile_rejects_purchase_evidence(self) -> None:
        rows = self.write_store(
            {
                "locations": [
                    {
                        "location_id": "loc-test",
                        "name": "Test location",
                        "parent_location_id": None,
                        "kind": "room",
                        "sensitivity": "personal",
                        "notes": None,
                    }
                ],
                "spatial_profiles": [
                    {
                        "profile_id": "spatial-test",
                        "location_id": "loc-test",
                        "profile_json": json.dumps(
                            {
                                "kind": "floor_rectangle",
                                "x": 0,
                                "y": 0,
                                "width": 1,
                                "height": 1,
                                "unit": "m",
                            }
                        ),
                        "evidence_id": "ev-test",
                        "sensitivity": "personal",
                        "notes": None,
                    }
                ],
            }
        )
        rows["evidence"][0].update(
            {"evidence_type": "merchant_account", "claim_strength": "purchase_only"}
        )
        for table, table_rows in rows.items():
            (self.store / f"{table}.jsonl").write_text(
                "".join(json.dumps(row, sort_keys=True) + "\n" for row in table_rows)
            )
        rejected = self.rebuild()
        self.assertNotEqual(rejected.returncode, 0)
        self.assertIn("invalid geometry evidence", rejected.stderr)

    def test_maintenance_sessions_record_system_and_multi_item_effort_once(self) -> None:
        rows = self.write_store()
        rows["items"].append(dict(rows["items"][0], item_id="itm-second"))
        rows["item_evidence"].append(
            {"item_id": "itm-second", "evidence_id": "ev-test", "role": "supporting"}
        )
        rows["maintenance_sessions"] = [
            {
                "maintenance_session_id": "maintenance-system",
                "performed_on": "2026-08-06",
                "activity": "system review",
                "elapsed_seconds": 30,
                "correction_count": 1,
                "review_count": 1,
                "evidence_id": "ev-test",
                "sensitivity": "personal",
                "notes": None,
            },
            {
                "maintenance_session_id": "maintenance-batch",
                "performed_on": "2026-08-06",
                "activity": "batch review",
                "elapsed_seconds": 60,
                "correction_count": 2,
                "review_count": 3,
                "evidence_id": "ev-test",
                "sensitivity": "personal",
                "notes": None,
            },
        ]
        rows["maintenance_session_items"] = [
            {"maintenance_session_id": "maintenance-batch", "item_id": "itm-test"},
            {"maintenance_session_id": "maintenance-batch", "item_id": "itm-second"},
        ]
        for table, table_rows in rows.items():
            (self.store / f"{table}.jsonl").write_text(
                "".join(json.dumps(row, sort_keys=True) + "\n" for row in table_rows)
            )
        rebuilt = self.rebuild()
        self.assertEqual(rebuilt.returncode, 0, rebuilt.stderr)
        with sqlite3.connect(self.database) as con:
            session_count, elapsed_total, correction_total, review_total = con.execute(
                "SELECT count(*), sum(elapsed_seconds), sum(correction_count), sum(review_count) "
                "FROM maintenance_sessions"
            ).fetchone()
            system_links = con.execute(
                "SELECT count(*) FROM maintenance_session_items "
                "WHERE maintenance_session_id='maintenance-system'"
            ).fetchone()[0]
            batch_links = con.execute(
                "SELECT count(*) FROM maintenance_session_items "
                "WHERE maintenance_session_id='maintenance-batch'"
            ).fetchone()[0]
        self.assertEqual(
            (session_count, elapsed_total, correction_total, review_total), (2, 90, 3, 4)
        )
        self.assertEqual((system_links, batch_links), (0, 2))

    def test_invalid_media_and_event_integrity_fails_closed(self) -> None:
        self.write_store(
            {
                "media_assets": [
                    {
                        "asset_id": "asset-test",
                        "sha256": "not-a-sha256",
                        "byte_size": -1,
                        "media_type": "image/jpeg",
                        "original_name": None,
                        "captured_on": None,
                        "sensitivity": "personal",
                        "uri": "media://wrong",
                    }
                ],
                "inventory_events": [
                    {
                        "event_id": "evt-test",
                        "sequence": 0,
                        "item_id": "itm-test",
                        "event_type": "physically_verified",
                        "occurred_on": "2026-08-05",
                        "observed_on": "2026-08-05",
                        "occurred_on_precision": "exact",
                        "actor": "Test",
                        "evidence_id": "ev-test",
                        "location_id": "loc-test",
                        "container_id": None,
                        "area_location_id": None,
                        "context_quality": "bound",
                        "details_json": None,
                        "notes": None,
                    }
                ],
            }
        )
        rebuilt = self.rebuild()
        self.assertNotEqual(rebuilt.returncode, 0)
        self.assertIn("CHECK constraint failed", rebuilt.stderr)

    def test_media_cannot_be_less_sensitive_than_its_supported_item(self) -> None:
        digest = hashlib.sha256(b"").hexdigest()
        self.write_store(
            {
                "media_assets": [
                    {
                        "asset_id": "asset-test",
                        "sha256": digest,
                        "byte_size": 0,
                        "media_type": "image/jpeg",
                        "original_name": "test.jpg",
                        "captured_on": "2026-08-05",
                        "sensitivity": "low",
                        "uri": f"media://sha256/{digest}",
                    }
                ]
            }
        )
        media_path = self.media / "sha256" / digest[:2] / digest
        media_path.parent.mkdir(parents=True)
        media_path.write_bytes(b"")
        self.assertEqual(self.rebuild().returncode, 0)
        self.assertEqual(
            self.command(
                RENDER,
                "--database",
                str(self.database),
                "--output",
                str(self.markdown),
            ).returncode,
            0,
        )
        verified = self.command(
            VERIFY,
            "--store",
            str(self.store),
            "--database",
            str(self.database),
            "--markdown",
            str(self.markdown),
            "--media-root",
            str(self.media),
        )
        self.assertNotEqual(verified.returncode, 0)
        self.assertIn("less sensitive than an item", verified.stdout)


if __name__ == "__main__":
    unittest.main()
