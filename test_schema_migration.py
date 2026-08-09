#!/usr/bin/env python3
"""Schema migration and sensitivity-scope acceptance tests."""

from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

from property_inventory import cli as cli_module

HERE = Path(__file__).resolve().parent
CLI = HERE / "property_inventory.py"
V1_TABLES = cli_module.V1_TABLES
V2_TABLES = cli_module.V2_TABLES
V3_TABLES = cli_module.V3_TABLES
V4_TABLES = cli_module.V4_TABLES
V5_TABLES = cli_module.V5_TABLES
V6_TABLES = cli_module.V6_TABLES
POST_V1_TABLES = tuple(table for table in V6_TABLES if table not in V1_TABLES)
POST_V2_TABLES = tuple(table for table in V6_TABLES if table not in V2_TABLES)
POST_V3_TABLES = tuple(table for table in V6_TABLES if table not in V3_TABLES)
POST_V4_TABLES = tuple(table for table in V6_TABLES if table not in V4_TABLES)
POST_V5_TABLES = tuple(table for table in V6_TABLES if table not in V5_TABLES)


class CliCase(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory(prefix="inventory-schema-v2-")
        self.scratch = Path(self.temp.name)
        self.root = self.scratch / "inventory"
        self.runtime = self.scratch / "runtime"
        self.assertEqual(self.cli("init")["status"], "initialized")

    def tearDown(self) -> None:
        self.temp.cleanup()

    @property
    def store(self) -> Path:
        return self.root / "Data" / "store"

    def command(self, *arguments: str) -> list[str]:
        return [
            sys.executable,
            str(CLI),
            "--inventory-root",
            str(self.root),
            "--runtime-dir",
            str(self.runtime),
            *arguments,
        ]

    def cli(self, *arguments: str) -> dict:
        completed = subprocess.run(
            self.command(*arguments), text=True, capture_output=True, check=False
        )
        self.assertEqual(completed.returncode, 0, completed.stderr or completed.stdout)
        return json.loads(completed.stdout)

    def cli_fails(self, *arguments: str) -> dict:
        completed = subprocess.run(
            self.command(*arguments), text=True, capture_output=True, check=False
        )
        self.assertNotEqual(completed.returncode, 0, completed.stdout)
        return json.loads(completed.stderr)

    def strip_pre_v6_event_context(self, *, remove_sequence: bool = False) -> None:
        """Make current fixture events faithful to the historical row shape."""
        events_path = self.store / "inventory_events.jsonl"
        events = [json.loads(line) for line in events_path.read_text().splitlines()]
        for event in events:
            if remove_sequence:
                event.pop("sequence")
            for field in (
                "location_id",
                "container_id",
                "area_location_id",
                "context_quality",
                "details_json",
                "observed_on",
                "occurred_on_precision",
            ):
                event.pop(field, None)
        events_path.write_text(
            "".join(json.dumps(event, sort_keys=True) + "\n" for event in events)
        )


class MigrationTest(CliCase):
    def test_v1_to_v6_preserves_rows_ids_and_unknowns(self) -> None:
        item_id = self.cli(
            "order",
            "--actor",
            "Migration fixture",
            "--source-ref",
            "Migration order fixture",
            "--name",
            "migration-unknown-object",
            "--category",
            "test fixture",
            "--ordered-on",
            "2026-08-05",
            "--order-placed",
        )["result"]["item_id"]
        self.assertIsNone(self.cli("show", item_id)["item"]["condition"])

        for table in POST_V1_TABLES:
            (self.store / f"{table}.jsonl").unlink()
        (self.root / ".property-inventory-runtime.json").write_text(
            json.dumps(
                {"format": 1, "runtime_dir": str(self.runtime.resolve())},
                sort_keys=True,
            )
            + "\n"
        )
        (self.runtime / ".property-inventory-owner.json").unlink()
        catalogue = self.root / "Inventory.md"
        catalogue.write_text(
            "\n".join(
                line
                for line in catalogue.read_text().splitlines()
                if "canonical-inventory-owner-sha256" not in line
            )
            + "\n"
        )
        self.strip_pre_v6_event_context(remove_sequence=True)
        before = {
            table: [
                json.loads(line)
                for line in (self.store / f"{table}.jsonl").read_text().splitlines()
            ]
            for table in V1_TABLES
        }
        self.assertIn("legacy inventory ownership is ambiguous", self.cli_fails("status")["error"])

        migrated = self.cli("migrate")
        self.assertEqual(migrated["result"]["from_schema"], 1)
        self.assertEqual(migrated["result"]["to_schema"], 6)
        self.assertTrue(Path(migrated["backup"]).is_dir())
        metadata = json.loads((self.store / "metadata.jsonl").read_text())
        self.assertEqual(metadata["schema_version"], 6)
        self.assertTrue(metadata["inventory_id"].startswith("inv-"))

        for table in V1_TABLES:
            after = [
                json.loads(line)
                for line in (self.store / f"{table}.jsonl").read_text().splitlines()
            ]
            if table == "inventory_events":
                self.assertEqual(
                    before[table],
                    [
                        {
                            key: value
                            for key, value in row.items()
                            if key
                            not in {
                                "sequence",
                                "location_id",
                                "container_id",
                                "area_location_id",
                                "context_quality",
                                "details_json",
                                "observed_on",
                                "occurred_on_precision",
                            }
                        }
                        for row in after
                    ],
                )
                self.assertEqual(
                    [row["sequence"] for row in after],
                    list(range(1, len(after) + 1)),
                )
            else:
                self.assertEqual(after, before[table])
        self.assertIsNone(self.cli("show", item_id)["item"]["condition"])
        self.assertEqual(self.cli("status")["verification"]["failures"], [])

    def test_v2_to_v6_preserves_inventory_identity_and_adds_receipts(self) -> None:
        metadata_path = self.store / "metadata.jsonl"
        metadata = json.loads(metadata_path.read_text())
        inventory_id = metadata["inventory_id"]
        metadata["schema_version"] = 2
        metadata_path.write_text(json.dumps(metadata, sort_keys=True) + "\n")
        for table in POST_V2_TABLES:
            (self.store / f"{table}.jsonl").unlink()
        before = {
            path.name: path.read_bytes()
            for path in sorted(self.store.glob("*.jsonl"))
            if path.name != "metadata.jsonl"
        }

        migrated = self.cli("migrate")
        self.assertEqual(migrated["result"]["from_schema"], 2)
        self.assertEqual(migrated["result"]["to_schema"], 6)
        current = json.loads(metadata_path.read_text())
        self.assertEqual(current["inventory_id"], inventory_id)
        self.assertEqual(current["schema_version"], 6)
        self.assertEqual((self.store / "proposal_commits.jsonl").read_text(), "")
        self.assertEqual(
            {
                path.name: path.read_bytes()
                for path in sorted(self.store.glob("*.jsonl"))
                if path.name
                not in {
                    "metadata.jsonl",
                    *(f"{table}.jsonl" for table in POST_V2_TABLES),
                }
            },
            before,
        )

    def test_v3_to_v6_preserves_existing_rows_and_adds_empty_structures(self) -> None:
        metadata_path = self.store / "metadata.jsonl"
        metadata = json.loads(metadata_path.read_text())
        metadata["schema_version"] = 3
        metadata_path.write_text(json.dumps(metadata, sort_keys=True) + "\n")
        for table in POST_V3_TABLES:
            (self.store / f"{table}.jsonl").unlink()
        before = {
            path.name: path.read_bytes()
            for path in sorted(self.store.glob("*.jsonl"))
            if path.name != "metadata.jsonl"
        }

        migrated = self.cli("migrate")
        self.assertEqual(migrated["result"]["from_schema"], 3)
        self.assertEqual(migrated["result"]["to_schema"], 6)
        self.assertEqual(json.loads(metadata_path.read_text())["schema_version"], 6)
        for table in POST_V3_TABLES:
            self.assertEqual((self.store / f"{table}.jsonl").read_text(), "")
        self.assertEqual(
            {
                path.name: path.read_bytes()
                for path in sorted(self.store.glob("*.jsonl"))
                if path.name
                not in {"metadata.jsonl", *(f"{table}.jsonl" for table in POST_V3_TABLES)}
            },
            before,
        )

    def test_v4_to_v6_marks_capture_history_unbound_without_runtime_inference(self) -> None:
        ordered = self.cli(
            "order",
            "--actor",
            "Migration fixture",
            "--source-ref",
            "Legacy capture evidence fixture",
            "--name",
            "legacy-capture-item",
            "--category",
            "test fixture",
            "--ordered-on",
            "2026-08-05",
            "--order-placed",
        )
        item_id = ordered["result"]["item_id"]
        item = self.cli("show", item_id)["item"]
        evidence_id = item["primary_evidence_id"]
        sensitivity = item["sensitivity"]
        metadata_path = self.store / "metadata.jsonl"
        metadata = json.loads(metadata_path.read_text())
        metadata["schema_version"] = 4
        metadata_path.write_text(json.dumps(metadata, sort_keys=True) + "\n")
        for table in POST_V4_TABLES:
            (self.store / f"{table}.jsonl").unlink()
        self.strip_pre_v6_event_context()
        legacy_observation = '{"barcode":"legacy-preserved"}'
        (self.store / "capture_sessions.jsonl").write_text(
            json.dumps(
                {
                    "capture_session_id": "capture-legacy",
                    "captured_on": "2026-08-05",
                    "evidence_id": evidence_id,
                    "sensitivity": sensitivity,
                    "notes": "legacy fixture",
                },
                sort_keys=True,
            )
            + "\n"
        )
        (self.store / "capture_observations.jsonl").write_text(
            json.dumps(
                {
                    "observation_id": "observation-legacy",
                    "capture_session_id": "capture-legacy",
                    "item_id": None,
                    "observation_json": legacy_observation,
                    "evidence_id": evidence_id,
                    "sensitivity": sensitivity,
                    "notes": "legacy fixture",
                },
                sort_keys=True,
            )
            + "\n"
        )

        migrated = self.cli("migrate")
        self.assertEqual(migrated["result"]["from_schema"], 4)
        self.assertEqual(migrated["result"]["to_schema"], 6)
        session = json.loads((self.store / "capture_sessions.jsonl").read_text())
        self.assertEqual(session["provenance_state"], "legacy_unbound")
        for field in (
            "artifact_json",
            "artifact_sha256",
            "review_json",
            "review_sha256",
        ):
            self.assertIsNone(session[field])
        observation = json.loads(
            (self.store / "capture_observations.jsonl").read_text()
        )
        self.assertEqual(observation["observation_json"], legacy_observation)
        self.assertEqual(observation["observation_index"], 1)
        self.assertEqual(observation["validation_state"], "legacy_unknown")
        self.assertEqual(self.cli("status")["verification"]["failures"], [])

    def test_v5_to_v6_preserves_rows_and_backfills_v6_context(self) -> None:
        ordered = self.cli(
            "order",
            "--actor", "Migration fixture",
            "--source-ref", "V5 preservation fixture",
            "--name", "v5-preserved-object",
            "--category", "test fixture",
            "--ordered-on", "2026-08-05",
            "--order-placed",
        )["result"]
        metadata_path = self.store / "metadata.jsonl"
        metadata = json.loads(metadata_path.read_text())
        metadata["schema_version"] = 5
        metadata_path.write_text(json.dumps(metadata, sort_keys=True) + "\n")
        for table in POST_V5_TABLES:
            (self.store / f"{table}.jsonl").unlink()
        items_path = self.store / "items.jsonl"
        legacy_items = [json.loads(line) for line in items_path.read_text().splitlines()]
        for item in legacy_items:
            item.pop("identity_sensitivity", None)
        items_path.write_text(
            "".join(json.dumps(item, sort_keys=True) + "\n" for item in legacy_items)
        )
        events_path = self.store / "inventory_events.jsonl"
        legacy_events = [json.loads(line) for line in events_path.read_text().splitlines()]
        for event in legacy_events:
            event.pop("location_id", None)
            event.pop("container_id", None)
            event.pop("area_location_id", None)
            event.pop("context_quality", None)
            event.pop("details_json", None)
            event.pop("observed_on", None)
            event.pop("occurred_on_precision", None)
        events_path.write_text(
            "".join(json.dumps(event, sort_keys=True) + "\n" for event in legacy_events)
        )

        migrated = self.cli("migrate")
        self.assertEqual(migrated["result"]["from_schema"], 5)
        self.assertEqual(migrated["result"]["to_schema"], 6)
        self.assertEqual(migrated["result"]["inventory_id"], metadata["inventory_id"])
        current_item = self.cli("show", ordered["item_id"])["item"]
        self.assertEqual(current_item["identity_sensitivity"], current_item["sensitivity"])
        current_event = json.loads(events_path.read_text().splitlines()[0])
        self.assertEqual(current_event["context_quality"], "legacy_unknown")
        self.assertEqual(current_event["occurred_on_precision"], "exact")
        self.assertEqual(current_event["observed_on"], current_event["occurred_on"])
        self.assertIsNone(current_event["location_id"])
        self.assertIsNone(current_event["details_json"])
        for table in POST_V5_TABLES:
            self.assertEqual((self.store / f"{table}.jsonl").read_text(), "")
        self.assertEqual(self.cli("status")["verification"]["failures"], [])

    def test_pre_upgrade_v3_pending_journal_recovers_before_v6_migration(self) -> None:
        item_id = self.cli(
            "order",
            "--actor",
            "Migration fixture",
            "--source-ref",
            "Migration order fixture",
            "--name",
            "pending-v3-object",
            "--category",
            "test fixture",
            "--ordered-on",
            "2026-08-05",
            "--order-placed",
        )["result"]["item_id"]
        metadata_path = self.store / "metadata.jsonl"
        metadata = json.loads(metadata_path.read_text())
        metadata["schema_version"] = 3
        metadata_path.write_text(json.dumps(metadata, sort_keys=True) + "\n")
        for table in POST_V3_TABLES:
            (self.store / f"{table}.jsonl").unlink()
        self.strip_pre_v6_event_context()
        evidence_path = self.store / "evidence.jsonl"
        evidence_path.write_text(
            "".join(
                json.dumps(
                    {key: value for key, value in json.loads(line).items() if key != "sensitivity"},
                    sort_keys=True,
                )
                + "\n"
                for line in evidence_path.read_text().splitlines()
            )
        )
        staged = self.scratch / "pre-upgrade-v3-staged"
        staged.mkdir()
        for table in cli_module.V3_TABLES:
            source = self.store / f"{table}.jsonl"
            (staged / source.name).write_bytes(source.read_bytes())
        items_path = staged / "items.jsonl"
        item = json.loads(items_path.read_text())
        item["notes"] = "staged v3 write"
        items_path.write_text(json.dumps(item, sort_keys=True) + "\n")
        paths = cli_module.data_paths(self.root, self.runtime)
        journal = cli_module.prepare_transaction(paths, staged, ["items.jsonl"])
        self.assertEqual(journal["schema_version"], 3)
        self.assertEqual(set(journal["target_generation"]), {f"{table}.jsonl" for table in cli_module.V3_TABLES})

        migrated = self.cli("migrate")
        self.assertEqual(migrated["result"]["from_schema"], 3)
        self.assertEqual(migrated["result"]["to_schema"], 6)
        self.assertEqual(self.cli("show", item_id)["item"]["item_id"], item_id)

    def test_v3_shared_evidence_backfill_uses_the_highest_supported_item_sensitivity(self) -> None:
        low = self.cli(
            "order", "--actor", "Migration fixture", "--source-ref", "low fixture",
            "--name", "low object", "--category", "test", "--ordered-on", "2026-08-05",
            "--order-placed", "--sensitivity", "low",
        )["result"]
        high = self.cli(
            "order", "--actor", "Migration fixture", "--source-ref", "high fixture",
            "--name", "high object", "--category", "test", "--ordered-on", "2026-08-05",
            "--order-placed", "--sensitivity", "high",
        )["result"]
        links_path = self.store / "item_evidence.jsonl"
        links = [json.loads(line) for line in links_path.read_text().splitlines()]
        links.append({"item_id": high["item_id"], "evidence_id": low["evidence_id"], "role": "supporting"})
        links_path.write_text("".join(json.dumps(row, sort_keys=True) + "\n" for row in links))
        metadata_path = self.store / "metadata.jsonl"
        metadata = json.loads(metadata_path.read_text())
        metadata["schema_version"] = 3
        metadata_path.write_text(json.dumps(metadata, sort_keys=True) + "\n")
        for table in POST_V3_TABLES:
            (self.store / f"{table}.jsonl").unlink()
        self.strip_pre_v6_event_context()
        evidence_path = self.store / "evidence.jsonl"
        evidence_path.write_text(
            "".join(
                json.dumps({key: value for key, value in json.loads(line).items() if key != "sensitivity"}, sort_keys=True)
                + "\n"
                for line in evidence_path.read_text().splitlines()
            )
        )

        self.cli("migrate")
        evidence = {
            row["evidence_id"]: row
            for row in (json.loads(line) for line in evidence_path.read_text().splitlines())
        }
        self.assertEqual(evidence[low["evidence_id"]]["sensitivity"], "high")

    def test_v1_migration_recovers_after_prepare_process_death(self) -> None:
        for table in POST_V1_TABLES:
            (self.store / f"{table}.jsonl").unlink()
        self.strip_pre_v6_event_context(remove_sequence=True)
        before = {
            path.name: path.read_bytes() for path in sorted(self.store.glob("*.jsonl"))
        }
        environment = dict(os.environ)
        environment["PROPERTY_INVENTORY_FAIL_AFTER_PREPARE"] = "1"
        crashed = subprocess.run(
            self.command("migrate"),
            text=True,
            capture_output=True,
            check=False,
            env=environment,
        )
        self.assertEqual(crashed.returncode, 96, crashed.stderr or crashed.stdout)
        self.assertEqual(
            {path.name: path.read_bytes() for path in sorted(self.store.glob("*.jsonl"))},
            before,
        )
        journal_path = self.runtime / ".property-inventory-transaction.json"
        journal = json.loads(journal_path.read_text())
        journal.pop("source_schema_version")
        journal_path.write_text(json.dumps(journal, sort_keys=True) + "\n")

        migrated = self.cli("migrate")
        self.assertEqual(migrated["result"]["from_schema"], 1)
        self.assertEqual(migrated["checks"]["verification"]["failures"], [])
        self.assertFalse(
            (self.runtime / ".property-inventory-transaction.json").exists()
        )

    def test_v2_migration_recovers_after_prepare_process_death(self) -> None:
        metadata_path = self.store / "metadata.jsonl"
        metadata = json.loads(metadata_path.read_text())
        inventory_id = metadata["inventory_id"]
        metadata["schema_version"] = 2
        metadata_path.write_text(json.dumps(metadata, sort_keys=True) + "\n")
        for table in POST_V2_TABLES:
            (self.store / f"{table}.jsonl").unlink()
        before = {
            path.name: path.read_bytes() for path in sorted(self.store.glob("*.jsonl"))
        }
        environment = dict(os.environ)
        environment["PROPERTY_INVENTORY_FAIL_AFTER_PREPARE"] = "1"
        crashed = subprocess.run(
            self.command("migrate"),
            text=True,
            capture_output=True,
            check=False,
            env=environment,
        )
        self.assertEqual(crashed.returncode, 96, crashed.stderr or crashed.stdout)
        self.assertEqual(
            {path.name: path.read_bytes() for path in sorted(self.store.glob("*.jsonl"))},
            before,
        )
        journal_path = self.runtime / ".property-inventory-transaction.json"
        journal = json.loads(journal_path.read_text())
        journal.pop("source_schema_version")
        journal_path.write_text(json.dumps(journal, sort_keys=True) + "\n")

        migrated = self.cli("migrate")
        self.assertEqual(migrated["result"]["from_schema"], 2)
        self.assertEqual(
            json.loads(metadata_path.read_text())["inventory_id"], inventory_id
        )
        self.assertEqual(migrated["checks"]["verification"]["failures"], [])
        self.assertFalse(
            (self.runtime / ".property-inventory-transaction.json").exists()
        )

    def test_v1_migration_recovers_after_partial_replacement(self) -> None:
        for table in POST_V1_TABLES:
            (self.store / f"{table}.jsonl").unlink()
        self.strip_pre_v6_event_context(remove_sequence=True)
        environment = dict(os.environ)
        environment["PROPERTY_INVENTORY_FAIL_AFTER_REPLACE"] = "1"
        crashed = subprocess.run(
            self.command("migrate"),
            text=True,
            capture_output=True,
            check=False,
            env=environment,
        )
        self.assertEqual(crashed.returncode, 97, crashed.stderr or crashed.stdout)

        migrated = self.cli("migrate")
        self.assertEqual(migrated["result"]["from_schema"], 1)
        self.assertEqual(migrated["checks"]["verification"]["failures"], [])
        self.assertFalse(
            (self.runtime / ".property-inventory-transaction.json").exists()
        )

    def test_migration_recovery_rejects_edit_to_unchanged_canonical_file(self) -> None:
        for table in POST_V1_TABLES:
            (self.store / f"{table}.jsonl").unlink()
        self.strip_pre_v6_event_context(remove_sequence=True)
        environment = dict(os.environ)
        environment["PROPERTY_INVENTORY_FAIL_AFTER_PREPARE"] = "1"
        crashed = subprocess.run(
            self.command("migrate"),
            text=True,
            capture_output=True,
            check=False,
            env=environment,
        )
        self.assertEqual(crashed.returncode, 96, crashed.stderr or crashed.stdout)
        locations_path = self.store / "locations.jsonl"
        locations_path.write_text(locations_path.read_text() + "\n")

        failure = self.cli_fails("migrate")
        self.assertIn("locations.jsonl", failure["error"])
        self.assertIn("outside the pending transaction", failure["error"])
        self.assertTrue(
            (self.runtime / ".property-inventory-transaction.json").exists()
        )

    def test_future_schema_refuses_reads_and_migration_without_store_mutation(self) -> None:
        metadata_path = self.store / "metadata.jsonl"
        metadata = json.loads(metadata_path.read_text())
        metadata["schema_version"] = 99
        metadata_path.write_text(json.dumps(metadata, sort_keys=True) + "\n")
        before = {
            path.name: path.read_bytes() for path in sorted(self.store.glob("*.jsonl"))
        }
        for command in (("status",), ("migrate",), ("search", "anything")):
            with self.subTest(command=command):
                failure = self.cli_fails(*command)
                self.assertIn("newer than supported schema 6", failure["error"])
                self.assertEqual(
                    {path.name: path.read_bytes() for path in sorted(self.store.glob("*.jsonl"))},
                    before,
                )

        metadata["schema_version"] = 6
        metadata_path.write_text(json.dumps(metadata, sort_keys=True) + "\n")
        workspace = self.runtime / ".property-inventory-transaction"
        workspace.mkdir()
        journal = self.runtime / ".property-inventory-transaction.json"
        journal.write_text(
            json.dumps(
                {
                    "format": 1,
                    "schema_version": 99,
                    "phase": "prepared",
                    "files": [],
                }
            )
            + "\n"
        )
        pending = self.cli_fails("status")
        self.assertIn("pending transaction schema 99 is newer", pending["error"])
        self.assertTrue(journal.exists())
        self.assertTrue(workspace.exists())


class ScopeTest(CliCase):
    def test_public_scope_excludes_high_items_and_redacts_private_fields(self) -> None:
        self.cli(
            "add-location",
            "--name",
            "Sensitive room",
            "--location-id",
            "loc-sensitive-room",
            "--kind",
            "room",
            "--sensitivity",
            "high",
        )
        self.cli(
            "add-location",
            "--name",
            "Low-labelled container in sensitive room",
            "--location-id",
            "loc-low-container",
            "--parent-location-id",
            "loc-sensitive-room",
            "--kind",
            "container",
            "--sensitivity",
            "low",
        )
        low_item = self.cli(
            "order",
            "--actor",
            "Scope fixture",
            "--source-ref",
            "Private order reference",
            "--notes",
            "Private item note",
            "--name",
            "scope-visible-object",
            "--category",
            "test fixture",
            "--ordered-on",
            "2026-08-05",
            "--order-placed",
            "--identifiers",
            '{"private_code":"secret"}',
            "--reference-url",
            "https://private.example/secret-token",
            "--location-id",
            "loc-sensitive-room",
            "--purchase-price",
            "10",
            "--purchase-currency",
            "GBP",
            "--receipt-ref",
            "private-receipt",
            "--sensitivity",
            "low",
        )["result"]["item_id"]
        self.cli(
            "receive",
            "--actor",
            "Scope fixture",
            "--source-ref",
            "Private physical reference",
            "--item-id",
            low_item,
            "--received-on",
            "2026-08-06",
            "--location-id",
            "loc-sensitive-room",
            "--container-id",
            "loc-low-container",
            "--serial-or-lot",
            "private-serial",
        )
        high_item = self.cli(
            "order",
            "--actor",
            "Scope fixture",
            "--source-ref",
            "High item order",
            "--name",
            "scope-hidden-object",
            "--category",
            "test fixture",
            "--ordered-on",
            "2026-08-05",
            "--order-placed",
            "--sensitivity",
            "high",
        )["result"]["item_id"]
        self.cli(
            "relate",
            "--subject-item-id",
            low_item,
            "--object-item-id",
            high_item,
            "--predicate",
            "unknown",
            "--confidence",
            "unknown",
            "--captured-on",
            "2026-08-06",
            "--evidence-type",
            "research",
            "--claim-strength",
            "research_only",
            "--source-ref",
            "Private relationship reference",
        )

        public = self.cli("--scope", "public", "show", low_item)
        self.assertEqual(public["location"], "[redacted]")
        self.assertEqual(public["container"], "[redacted]")
        self.assertIsNone(public["item"]["location_id"])
        self.assertIsNone(public["item"]["container_id"])
        self.assertIsNone(public["item"]["serial_or_lot"])
        self.assertIsNone(public["item"]["receipt_ref"])
        self.assertIsNone(public["item"]["purchase_price"])
        self.assertIsNone(public["item"]["notes"])
        self.assertEqual(public["model"]["identifiers"], {})
        self.assertIsNone(public["model"]["reference_url"])
        self.assertEqual(public["evidence_ids"], [])
        self.assertEqual(public["relationships"], [])
        self.assertTrue(all(event["notes"] is None for event in public["events"]))
        self.assertFalse(
            self.cli("--scope", "public", "search", "private-serial")["recorded"]
        )
        self.assertFalse(
            self.cli("--scope", "public", "search", "scope-hidden-object")["recorded"]
        )
        hidden_error = self.cli_fails("--scope", "public", "show", high_item)["error"]
        absent_error = self.cli_fails(
            "--scope", "public", "show", "itm-not-recorded"
        )["error"]
        self.assertEqual(hidden_error, "inventory command could not complete safely in this scope")
        self.assertEqual(absent_error, hidden_error)
        public_status = self.cli("--scope", "public", "status")
        self.assertEqual(
            set(public_status),
            {"status", "scope", "store_valid", "recovery"},
        )
        self.assertNotIn("verification", public_status)

        private = self.cli("--scope", "private", "show", low_item)
        self.assertEqual(private["location"], "Sensitive room")
        self.assertEqual(private["item"]["serial_or_lot"], "private-serial")
        self.assertEqual(private["item"]["receipt_ref"], "private-receipt")
        self.assertEqual(private["model"]["identifiers"]["private_code"], "secret")
        self.assertEqual(
            private["model"]["reference_url"],
            "https://private.example/secret-token",
        )
        self.assertEqual(len(private["relationships"]), 1)


if __name__ == "__main__":
    unittest.main()
