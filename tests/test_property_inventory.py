#!/usr/bin/env python3
"""End-to-end acceptance tests for the transactional inventory CLI."""

from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import threading
import unittest
from pathlib import Path
from unittest.mock import patch

from filelock import FileLock

import property_inventory as legacy_inventory
from property_inventory import InventoryError, canonical_lock_path, git_store_is_clean

HERE = Path(__file__).resolve().parents[1]
CLI = HERE / "property_inventory.py"


class PropertyInventoryLifecycleTest(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory(prefix="inventory-acceptance-")
        self.scratch = Path(self.temp.name)
        self.root = self.scratch / "inventory"
        self.runtime = self.scratch / "runtime"
        initialized = self.cli("init")
        self.assertEqual(initialized["status"], "initialized")
        self.assertEqual(initialized["checks"]["verification"]["status"], "pass")

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

    def test_order_delivery_and_sale_pass_on_the_first_run(self) -> None:
        self.cli(
            "add-location",
            "--name",
            "Sample region",
            "--location-id",
            "loc-sample-region",
            "--kind",
            "place",
            "--sensitivity",
            "high",
        )
        self.cli(
            "add-location",
            "--name",
            "Sample apartment",
            "--location-id",
            "loc-sample-apartment",
            "--parent-location-id",
            "loc-sample-region",
            "--kind",
            "place",
            "--sensitivity",
            "high",
        )

        before = self.cli("search", "acceptance-fixture-object")
        self.assertFalse(before["recorded"])
        self.assertEqual(before["meaning_if_empty"], "unknown, not absent")

        ordered = self.cli(
            "order",
            "--actor",
            "Cold agent",
            "--source-ref",
            "Acceptance fixture order confirmation",
            "--name",
            "acceptance-fixture-object",
            "--category",
            "test fixture",
            "--ordered-on",
            "2026-08-05",
            "--order-placed",
            "--quantity",
            "1",
            "--location-id",
            "loc-sample-apartment",
        )
        self.assertEqual(ordered["checks"]["verification"]["failures"], [])
        item_id = ordered["result"]["item_id"]

        duplicate = self.cli_fails(
            "order",
            "--actor",
            "Cold agent",
            "--source-ref",
            "Second fixture order confirmation",
            "--name",
            "acceptance-fixture-object",
            "--category",
            "test fixture",
            "--ordered-on",
            "2026-08-05",
            "--order-placed",
        )
        self.assertIn("--existing-item-id", duplicate["error"])

        received = self.cli(
            "receive",
            "--actor",
            "Cold agent",
            "--source-ref",
            "Acceptance fixture physically received",
            "--item-id",
            item_id,
            "--received-on",
            "2026-08-06",
            "--location-id",
            "loc-sample-apartment",
            "--condition",
            "new",
            "--physical-check",
        )
        self.assertEqual(received["checks"]["verification"]["failures"], [])

        companion = self.cli(
            "order",
            "--actor",
            "Cold agent",
            "--source-ref",
            "Acceptance companion order confirmation",
            "--name",
            "acceptance-companion-object",
            "--category",
            "test fixture",
            "--ordered-on",
            "2026-08-06",
            "--order-placed",
        )["result"]["item_id"]
        related = self.cli(
            "relate",
            "--subject-item-id",
            item_id,
            "--object-item-id",
            companion,
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
            "Acceptance fixture relationship",
        )
        self.assertTrue(related["result"]["relationship_id"])
        self.assertTrue(related["result"]["evidence_id"])

        absent = self.cli(
            "not-found",
            "--actor",
            "Cold agent",
            "--source-ref",
            "Acceptance fixture sample-apartment sweep",
            "--item-id",
            item_id,
            "--area-location-id",
            "loc-sample-apartment",
            "--checked-on",
            "2026-08-07",
        )
        self.assertEqual(absent["result"]["ownership_state"], "confirmed")
        self.assertTrue(absent["result"]["follow_up_required"])

        sold = self.cli(
            "sell",
            "--actor",
            "Cold agent",
            "--source-ref",
            "Acceptance fixture sale confirmation",
            "--item-id",
            item_id,
            "--sold-on",
            "2026-08-07",
        )
        self.assertEqual(sold["checks"]["verification"]["failures"], [])

        shown = self.cli("show", item_id)
        self.assertEqual(shown["item"]["ownership_state"], "disposed")
        self.assertIsNone(shown["item"]["location_id"])
        self.assertEqual(
            [event["event_type"] for event in shown["events"][-5:]],
            ["ordered", "received", "physically_verified", "not_found_in_area", "sold"],
        )
        evidence = [
            json.loads(line)
            for line in (self.store / "evidence.jsonl").read_text().splitlines()
        ]
        physical = [
            row
            for row in evidence
            if row["evidence_type"] == "physical_check"
            and row["source_ref"] == "Acceptance fixture physically received"
        ]
        self.assertEqual(len(physical), 1)
        self.assertEqual(physical[0]["claim_strength"], "explicit_current")
        self.assertGreaterEqual(len(list((self.runtime / "backups").glob("20*"))), 7)

    def test_cart_is_not_an_order_and_is_reused_at_checkout(self) -> None:
        planned = self.cli(
            "plan",
            "--actor",
            "Cold agent",
            "--source-ref",
            "Acceptance fixture shopping cart",
            "--name",
            "acceptance-planned-object",
            "--category",
            "test fixture",
            "--planned-on",
            "2026-08-05",
            "--quantity",
            "1",
        )
        item_id = planned["result"]["item_id"]
        self.assertEqual(self.cli("show", item_id)["item"]["ownership_state"], "planned")

        duplicate = self.cli_fails(
            "order",
            "--actor",
            "Cold agent",
            "--source-ref",
            "Acceptance fixture order confirmation",
            "--name",
            "acceptance-planned-object",
            "--category",
            "test fixture",
            "--ordered-on",
            "2026-08-05",
            "--order-placed",
        )
        self.assertIn("--existing-item-id", duplicate["error"])

        ordered = self.cli(
            "order",
            "--actor",
            "Cold agent",
            "--source-ref",
            "Acceptance fixture order confirmation",
            "--name",
            "acceptance-planned-object",
            "--category",
            "test fixture",
            "--ordered-on",
            "2026-08-05",
            "--order-placed",
            "--existing-item-id",
            item_id,
        )
        self.assertFalse(ordered["result"]["created"])
        shown = self.cli("show", item_id)
        self.assertEqual(shown["item"]["ownership_state"], "candidate")
        self.assertEqual(
            [event["event_type"] for event in shown["events"][-2:]],
            ["planned", "ordered"],
        )
        self.assertEqual(self.cli("search", "acceptance-planned-object")["count"], 1)

    def test_replanning_quantity_appends_an_exact_replayable_payload(self) -> None:
        planned = self.cli(
            "plan", "--actor", "Cold agent", "--source-ref", "Initial cart quantity",
            "--name", "acceptance-replanned-object", "--category", "test fixture",
            "--planned-on", "2026-08-05", "--quantity", "1",
        )
        item_id = planned["result"]["item_id"]
        replanned = self.cli(
            "plan", "--actor", "Cold agent", "--source-ref", "Updated cart quantity",
            "--name", "acceptance-replanned-object", "--category", "test fixture",
            "--planned-on", "2026-08-06", "--quantity", "3",
            "--existing-item-id", item_id,
        )
        self.assertEqual(replanned["checks"]["verification"]["failures"], [])
        shown = self.cli("show", item_id)
        self.assertEqual(shown["item"]["quantity"], 3)
        self.assertEqual(
            json.loads(shown["events"][-1]["details_json"]),
            {
                "previous_quantity": 1,
                "previous_unit": "item",
                "quantity": 3,
                "unit": "item",
            },
        )

    def test_ordering_an_existing_plan_records_quantity_price_and_receipt_history(self) -> None:
        planned = self.cli(
            "plan", "--actor", "Cold agent", "--source-ref", "Cart fixture",
            "--name", "acceptance-checkout-object", "--category", "test fixture",
            "--planned-on", "2026-08-05", "--quantity", "1",
        )
        item_id = planned["result"]["item_id"]
        ordered = self.cli(
            "order", "--actor", "Cold agent", "--source-ref", "Checkout fixture",
            "--name", "acceptance-checkout-object", "--category", "test fixture",
            "--ordered-on", "2026-08-06", "--order-placed",
            "--existing-item-id", item_id, "--quantity", "2",
            "--purchase-price", "24.50", "--purchase-currency", "GBP",
            "--receipt-ref", "receipt-fixture",
        )
        self.assertEqual(ordered["checks"]["verification"]["failures"], [])
        self.assertIsNotNone(ordered["result"]["detail_amendment_id"])
        shown = self.cli("show", item_id)
        self.assertEqual(shown["item"]["quantity"], 2)
        self.assertEqual(shown["item"]["purchase_price"], 24.5)
        self.assertEqual(shown["item"]["purchase_currency"], "GBP")
        self.assertEqual(shown["item"]["receipt_ref"], "receipt-fixture")
        self.assertEqual(
            json.loads(shown["events"][-1]["details_json"]),
            {
                "previous_quantity": 1,
                "previous_unit": "item",
                "quantity": 2,
                "unit": "item",
            },
        )

    def test_bulk_runtime_artifacts_stay_outside_the_inventory_root(self) -> None:
        self.cli("status")
        files = {
            path.relative_to(self.root).as_posix()
            for path in self.root.rglob("*")
            if path.is_file()
        }
        expected = {"Inventory.md", ".gitignore", ".property-inventory-runtime.json"} | {
            f"Data/store/{table}.jsonl"
            for table in (
                "metadata",
                "proposal_commits",
                "locations",
                "models",
                "evidence",
                "media_assets",
                "interfaces",
                "items",
                "item_evidence",
                "evidence_assets",
                "model_interfaces",
                "relationships",
                "item_documents",
                "torque_paths",
                "kits",
                "kit_requirements",
                "item_tags",
                "aliases",
                "spatial_profiles",
                "valuations",
                "capture_sessions",
                "capture_observations",
                "maintenance_sessions",
                "maintenance_session_items",
                "sync_receipts",
                "kit_reviews",
                "item_dimensions",
                "item_amendments",
                "item_detail_amendments",
                "fact_amendments",
                "parties",
                "item_party_relations",
                "location_embodiments",
                "inventory_events",
            )
        }
        self.assertEqual(files, expected)

    def test_status_preserves_created_property_and_repeats_byte_identically(self) -> None:
        catalogue = self.root / "Inventory.md"
        initial = catalogue.read_text()
        current_created = re.search(
            r"^Created: \d{4}-\d{2}-\d{2}$", initial, re.MULTILINE
        )
        self.assertIsNotNone(current_created)
        historical = initial.replace(current_created.group(0), "Created: 2020-01-02")
        catalogue.write_text(historical)

        first = self.cli("status")
        self.assertEqual(first["status"], "pass")
        self.assertEqual(catalogue.read_text(), historical)
        first_bytes = catalogue.read_bytes()

        second = self.cli("status")
        self.assertEqual(second["status"], "pass")
        self.assertEqual(catalogue.read_bytes(), first_bytes)
        self.assertTrue((self.runtime / "inventory.sqlite").exists())
        self.assertTrue((self.runtime / "backups").is_dir())

    def test_runtime_rebind_requires_a_quiescent_matching_old_runtime(self) -> None:
        new_runtime = self.scratch / "new-runtime"
        command = [
            sys.executable,
            str(CLI),
            "--inventory-root",
            str(self.root),
            "--runtime-dir",
            str(new_runtime),
            "runtime-rebind",
            "--from-runtime",
            str(self.runtime),
        ]
        completed = subprocess.run(command, text=True, capture_output=True, check=False)
        self.assertEqual(completed.returncode, 0, completed.stderr or completed.stdout)
        rebound = json.loads(completed.stdout)
        self.assertEqual(rebound["status"], "rebound")
        self.assertEqual(rebound["checks"]["verification"]["failures"], [])
        self.assertTrue(self.runtime.is_dir())
        self.assertEqual(
            (self.root / ".gitignore").read_text(),
            "/.property-inventory-runtime.json\n",
        )
        self.runtime = new_runtime
        self.assertEqual(self.cli("status")["verification"]["failures"], [])

        blocked_runtime = self.scratch / "blocked-runtime"
        pending = new_runtime / ".property-inventory-transaction.json"
        pending.write_text("{}")
        blocked_command = [
            sys.executable,
            str(CLI),
            "--inventory-root",
            str(self.root),
            "--runtime-dir",
            str(blocked_runtime),
            "runtime-rebind",
            "--from-runtime",
            str(new_runtime),
        ]
        blocked = subprocess.run(
            blocked_command, text=True, capture_output=True, check=False
        )
        self.assertNotEqual(blocked.returncode, 0)
        self.assertIn("pending recovery state", json.loads(blocked.stderr)["error"])
        self.assertFalse(blocked_runtime.exists())
        pending.unlink()

        pending.symlink_to(new_runtime / "missing-journal-target")
        broken_link_runtime = self.scratch / "broken-link-runtime"
        broken_link_command = [
            sys.executable,
            str(CLI),
            "--inventory-root",
            str(self.root),
            "--runtime-dir",
            str(broken_link_runtime),
            "runtime-rebind",
            "--from-runtime",
            str(new_runtime),
        ]
        broken_link = subprocess.run(
            broken_link_command, text=True, capture_output=True, check=False
        )
        self.assertNotEqual(broken_link.returncode, 0)
        self.assertIn(
            "pending recovery state", json.loads(broken_link.stderr)["error"]
        )
        self.assertTrue(pending.is_symlink())
        self.assertFalse(broken_link_runtime.exists())
        self.assertIn("symlink", self.cli_fails("status")["error"])
        pending.unlink()

        operations = self.scratch / "prepared-rebind-operations.json"
        operations.write_text(
            json.dumps(
                [
                    [
                        "add-location",
                        "--name",
                        "Prepared rebind location",
                        "--kind",
                        "room",
                    ]
                ]
            )
        )
        self.assertEqual(
            self.cli("propose", "--operations", str(operations))["status"],
            "prepared",
        )
        proposal_blocked_runtime = self.scratch / "proposal-blocked-runtime"
        proposal_blocked = subprocess.run(
            [
                sys.executable,
                str(CLI),
                "--inventory-root",
                str(self.root),
                "--runtime-dir",
                str(proposal_blocked_runtime),
                "runtime-rebind",
                "--from-runtime",
                str(new_runtime),
            ],
            text=True,
            capture_output=True,
            check=False,
        )
        self.assertNotEqual(proposal_blocked.returncode, 0)
        self.assertIn(
            "prepared proposals", json.loads(proposal_blocked.stderr)["error"]
        )
        self.assertFalse(proposal_blocked_runtime.exists())

    def test_runtime_rebind_allows_the_documented_legacy_then_migrate_order(self) -> None:
        metadata_path = self.store / "metadata.jsonl"
        metadata = json.loads(metadata_path.read_text())
        metadata["schema_version"] = 4
        metadata_path.write_text(json.dumps(metadata, sort_keys=True) + "\n")
        for table in legacy_inventory._CLI.V7_TABLES:
            if table not in legacy_inventory._CLI.V4_TABLES:
                (self.store / f"{table}.jsonl").unlink()

        new_catalogue = self.scratch / "renamed-vault" / "Inventory.md"
        completed = subprocess.run(
            [
                sys.executable,
                str(CLI),
                "--inventory-root",
                str(self.root),
                "--runtime-dir",
                str(self.runtime),
                "--catalogue-output",
                str(new_catalogue),
                "--scope",
                "private",
                "runtime-rebind",
                "--from-runtime",
                str(self.runtime),
                "--from-catalogue-output",
                str(self.root / "Inventory.md"),
            ],
            text=True,
            capture_output=True,
            check=False,
        )
        self.assertEqual(completed.returncode, 0, completed.stderr or completed.stdout)
        rebound = json.loads(completed.stdout)
        self.assertEqual(rebound["status"], "rebound")
        self.assertEqual(rebound["checks"]["status"], "migration_required")
        self.assertEqual(rebound["checks"]["schema_version"], 4)
        self.assertEqual(rebound["checks"]["target_schema_version"], 7)
        self.assertFalse(new_catalogue.exists())

        migrated = self.cli("--catalogue-output", str(new_catalogue), "migrate")
        self.assertEqual(migrated["result"]["from_schema"], 4)
        self.assertEqual(migrated["result"]["to_schema"], 7)
        self.assertEqual(
            self.cli("--catalogue-output", str(new_catalogue), "status")[
                "verification"
            ]["failures"],
            [],
        )

    def test_runtime_rebind_rejects_a_legacy_store_that_cannot_migrate(self) -> None:
        metadata_path = self.store / "metadata.jsonl"
        metadata = json.loads(metadata_path.read_text())
        metadata["schema_version"] = 4
        metadata_path.write_text(json.dumps(metadata, sort_keys=True) + "\n")
        for table in legacy_inventory._CLI.V7_TABLES:
            if table not in legacy_inventory._CLI.V4_TABLES:
                (self.store / f"{table}.jsonl").unlink()
        locations_path = self.store / "locations.jsonl"
        location = locations_path.read_text().splitlines()[0]
        locations_path.write_text(locations_path.read_text() + location + "\n")
        owner_path = self.runtime / ".property-inventory-owner.json"
        owner_before = owner_path.read_bytes()
        new_catalogue = self.scratch / "invalid-legacy-vault" / "Inventory.md"

        completed = subprocess.run(
            [
                sys.executable,
                str(CLI),
                "--inventory-root",
                str(self.root),
                "--runtime-dir",
                str(self.runtime),
                "--catalogue-output",
                str(new_catalogue),
                "--scope",
                "private",
                "runtime-rebind",
                "--from-runtime",
                str(self.runtime),
                "--from-catalogue-output",
                str(self.root / "Inventory.md"),
            ],
            text=True,
            capture_output=True,
            check=False,
        )
        self.assertNotEqual(completed.returncode, 0)
        self.assertIn("cannot complete its declared migration", completed.stderr)
        self.assertEqual(owner_path.read_bytes(), owner_before)
        self.assertFalse(new_catalogue.exists())

    def test_runtime_rebind_rejects_a_quarantined_legacy_store(self) -> None:
        metadata_path = self.store / "metadata.jsonl"
        metadata = json.loads(metadata_path.read_text())
        metadata["schema_version"] = 4
        metadata_path.write_text(json.dumps(metadata, sort_keys=True) + "\n")
        for table in legacy_inventory._CLI.V7_TABLES:
            if table not in legacy_inventory._CLI.V4_TABLES:
                (self.store / f"{table}.jsonl").unlink()
        (self.root / legacy_inventory._CLI.DEGRADED_MARKER).write_text(
            json.dumps(
                {"format": 1, "reasons": ["quarantined test restore"]},
                sort_keys=True,
            )
            + "\n"
        )
        owner_path = self.runtime / ".property-inventory-owner.json"
        owner_before = owner_path.read_bytes()
        new_catalogue = self.scratch / "quarantined-legacy-vault" / "Inventory.md"

        completed = subprocess.run(
            [
                sys.executable,
                str(CLI),
                "--inventory-root",
                str(self.root),
                "--runtime-dir",
                str(self.runtime),
                "--catalogue-output",
                str(new_catalogue),
                "--scope",
                "private",
                "runtime-rebind",
                "--from-runtime",
                str(self.runtime),
                "--from-catalogue-output",
                str(self.root / "Inventory.md"),
            ],
            text=True,
            capture_output=True,
            check=False,
        )
        self.assertNotEqual(completed.returncode, 0)
        self.assertIn("quarantined by a degraded restore", completed.stderr)
        self.assertEqual(owner_path.read_bytes(), owner_before)
        self.assertFalse(new_catalogue.exists())

    def test_runtime_rebind_preserves_current_schema_degraded_recovery_access(self) -> None:
        (self.root / legacy_inventory._CLI.DEGRADED_MARKER).write_text(
            json.dumps(
                {"format": 1, "reasons": ["current-schema recovery fixture"]},
                sort_keys=True,
            )
            + "\n"
        )
        new_catalogue = self.scratch / "current-degraded-vault" / "Inventory.md"
        completed = subprocess.run(
            [
                sys.executable,
                str(CLI),
                "--inventory-root",
                str(self.root),
                "--runtime-dir",
                str(self.runtime),
                "--catalogue-output",
                str(new_catalogue),
                "--scope",
                "private",
                "runtime-rebind",
                "--from-runtime",
                str(self.runtime),
                "--from-catalogue-output",
                str(self.root / "Inventory.md"),
            ],
            text=True,
            capture_output=True,
            check=False,
        )
        self.assertEqual(completed.returncode, 0, completed.stderr or completed.stdout)
        rebound = json.loads(completed.stdout)
        self.assertEqual(rebound["status"], "rebound")
        self.assertEqual(rebound["checks"]["status"], "degraded_unsafe_legacy")
        self.assertTrue(new_catalogue.is_file())

    def test_v1_rebind_promotes_the_new_owner_identity_and_recovers_null_owner(self) -> None:
        for table in legacy_inventory._CLI.V7_TABLES:
            if table not in legacy_inventory._CLI.V1_TABLES:
                (self.store / f"{table}.jsonl").unlink()
        owner_path = self.runtime / ".property-inventory-owner.json"
        owner = json.loads(owner_path.read_text())
        owner["inventory_id"] = None
        owner_path.write_text(json.dumps(owner, indent=2, sort_keys=True) + "\n")
        new_catalogue = self.scratch / "v1-renamed-vault" / "Inventory.md"
        rebind = subprocess.run(
            [
                sys.executable,
                str(CLI),
                "--inventory-root",
                str(self.root),
                "--runtime-dir",
                str(self.runtime),
                "--catalogue-output",
                str(new_catalogue),
                "--scope",
                "private",
                "runtime-rebind",
                "--from-runtime",
                str(self.runtime),
                "--from-catalogue-output",
                str(self.root / "Inventory.md"),
            ],
            text=True,
            capture_output=True,
            check=False,
        )
        self.assertEqual(rebind.returncode, 0, rebind.stderr or rebind.stdout)
        self.assertEqual(json.loads(rebind.stdout)["checks"]["schema_version"], 1)

        migrated = self.cli("--catalogue-output", str(new_catalogue), "migrate")
        inventory_id = migrated["result"]["inventory_id"]
        self.assertEqual(json.loads(owner_path.read_text())["inventory_id"], inventory_id)
        self.assertEqual(
            self.cli("--catalogue-output", str(new_catalogue), "status")["status"],
            "pass",
        )

        owner = json.loads(owner_path.read_text())
        owner["inventory_id"] = None
        owner_path.write_text(json.dumps(owner, indent=2, sort_keys=True) + "\n")
        recovered = self.cli("--catalogue-output", str(new_catalogue), "migrate")
        self.assertEqual(recovered["status"], "already_current")
        self.assertEqual(json.loads(owner_path.read_text())["inventory_id"], inventory_id)
        self.assertEqual(
            self.cli("--catalogue-output", str(new_catalogue), "status")["status"],
            "pass",
        )

    def test_runtime_rebind_safely_updates_a_renamed_catalogue(self) -> None:
        old_catalogue = self.root / "Inventory.md"
        new_catalogue = self.scratch / "renamed-notes" / "Inventory.md"
        base = [
            sys.executable,
            str(CLI),
            "--inventory-root",
            str(self.root),
            "--runtime-dir",
            str(self.runtime),
            "--catalogue-output",
            str(new_catalogue),
            "--scope",
            "private",
            "runtime-rebind",
            "--from-runtime",
            str(self.runtime),
        ]

        wrong = subprocess.run(
            [*base, "--from-catalogue-output", str(self.scratch / "wrong.md")],
            text=True,
            capture_output=True,
            check=False,
        )
        self.assertNotEqual(wrong.returncode, 0)
        self.assertIn("does not match the runtime owner marker", wrong.stderr)
        self.assertFalse(new_catalogue.exists())

        completed = subprocess.run(
            [*base, "--from-catalogue-output", str(old_catalogue)],
            text=True,
            capture_output=True,
            check=False,
        )
        self.assertEqual(completed.returncode, 0, completed.stderr or completed.stdout)
        rebound = json.loads(completed.stdout)
        self.assertEqual(rebound["status"], "rebound")
        self.assertEqual(rebound["checks"]["verification"]["failures"], [])
        self.assertEqual(rebound["catalogue_output"], str(new_catalogue.resolve()))
        self.assertTrue(new_catalogue.is_file())
        owner = json.loads(
            (self.runtime / ".property-inventory-owner.json").read_text()
        )
        self.assertEqual(owner["catalogue_output"], str(new_catalogue.resolve()))

        repeated = subprocess.run(
            [*base, "--from-catalogue-output", str(old_catalogue)],
            text=True,
            capture_output=True,
            check=False,
        )
        self.assertEqual(repeated.returncode, 0, repeated.stderr or repeated.stdout)
        self.assertEqual(json.loads(repeated.stdout)["status"], "already_bound")

        status = subprocess.run(
            [
                sys.executable,
                str(CLI),
                "--inventory-root",
                str(self.root),
                "--runtime-dir",
                str(self.runtime),
                "--catalogue-output",
                str(new_catalogue),
                "status",
            ],
            text=True,
            capture_output=True,
            check=False,
        )
        self.assertEqual(status.returncode, 0, status.stderr or status.stdout)
        self.assertEqual(json.loads(status.stdout)["verification"]["failures"], [])

    def test_runtime_rebind_safely_updates_a_renamed_media_root(self) -> None:
        root = self.scratch / "media-rebind-inventory"
        runtime = self.scratch / "media-rebind-runtime"
        old_media = self.scratch / "old-media"
        new_media = self.scratch / "new-media"
        prefix = [
            sys.executable,
            str(CLI),
            "--inventory-root",
            str(root),
            "--runtime-dir",
            str(runtime),
        ]
        initialized = subprocess.run(
            [*prefix, "--media-root", str(old_media), "init"],
            text=True,
            capture_output=True,
            check=False,
        )
        self.assertEqual(
            initialized.returncode, 0, initialized.stderr or initialized.stdout
        )

        def old_cli(*arguments: str) -> dict:
            completed = subprocess.run(
                [*prefix, "--media-root", str(old_media), *arguments],
                text=True,
                capture_output=True,
                check=False,
            )
            self.assertEqual(
                completed.returncode, 0, completed.stderr or completed.stdout
            )
            return json.loads(completed.stdout)

        old_cli(
            "add-location",
            "--name",
            "Media rebind room",
            "--location-id",
            "loc-media-rebind-room",
            "--kind",
            "room",
        )
        item_id = old_cli(
            "order",
            "--actor",
            "Media rebind test",
            "--source-ref",
            "Media rebind order",
            "--name",
            "media-rebind-object",
            "--category",
            "test fixture",
            "--ordered-on",
            "2026-08-09",
            "--order-placed",
        )["result"]["item_id"]
        evidence_id = old_cli(
            "receive",
            "--actor",
            "Media rebind test",
            "--source-ref",
            "Media rebind physical check",
            "--item-id",
            item_id,
            "--received-on",
            "2026-08-09",
            "--location-id",
            "loc-media-rebind-room",
            "--physical-check",
        )["result"]["evidence_id"]
        source = self.scratch / "media-rebind-source.bin"
        source.write_bytes(b"media rebind fixture")
        digest = old_cli(
            "attach-media",
            "--evidence-id",
            evidence_id,
            "--file",
            str(source),
            "--role",
            "source",
            "--captured-on",
            "2026-08-09",
            "--media-type",
            "application/octet-stream",
        )["result"]["sha256"]
        old_media.rename(new_media)

        rebound = subprocess.run(
            [
                *prefix,
                "--media-root",
                str(new_media),
                "--scope",
                "private",
                "runtime-rebind",
                "--from-runtime",
                str(runtime),
                "--from-media-root",
                str(old_media),
            ],
            text=True,
            capture_output=True,
            check=False,
        )
        self.assertEqual(rebound.returncode, 0, rebound.stderr or rebound.stdout)
        result = json.loads(rebound.stdout)
        self.assertEqual(result["status"], "rebound")
        self.assertEqual(result["checks"]["verification"]["failures"], [])
        self.assertEqual(result["media_root"], str(new_media.resolve()))
        owner = json.loads((runtime / ".property-inventory-owner.json").read_text())
        self.assertEqual(owner["media_root"], str(new_media.resolve()))
        self.assertTrue((new_media / "sha256" / digest[:2] / digest).is_file())

    def test_runtime_rebind_blocks_a_writer_validated_against_old_paths(self) -> None:
        cli_module = legacy_inventory._CLI
        root = self.scratch / "interleaved-rebind-inventory"
        runtime = self.scratch / "interleaved-rebind-runtime"
        old_media = self.scratch / "interleaved-old-media"
        new_media = self.scratch / "interleaved-new-media"
        old_catalogue = self.scratch / "interleaved-old-catalogue.md"
        new_catalogue = self.scratch / "interleaved-new-catalogue.md"
        common = [
            "--inventory-root",
            str(root),
            "--runtime-dir",
            str(runtime),
            "--media-root",
            str(old_media),
            "--catalogue-output",
            str(old_catalogue),
        ]
        cli_module.execute([*common, "init"])
        cli_module.execute(
            [
                *common,
                "add-location",
                "--name",
                "Interleaved rebind room",
                "--location-id",
                "loc-interleaved-rebind-room",
                "--kind",
                "room",
            ]
        )
        item_id = cli_module.execute(
            [
                *common,
                "order",
                "--actor",
                "Interleaving test",
                "--source-ref",
                "Interleaving order",
                "--name",
                "interleaved-rebind-object",
                "--category",
                "test fixture",
                "--ordered-on",
                "2026-08-09",
                "--order-placed",
            ]
        )["result"]["item_id"]
        evidence_id = cli_module.execute(
            [
                *common,
                "receive",
                "--actor",
                "Interleaving test",
                "--source-ref",
                "Interleaving physical check",
                "--item-id",
                item_id,
                "--received-on",
                "2026-08-09",
                "--location-id",
                "loc-interleaved-rebind-room",
                "--physical-check",
            ]
        )["result"]["evidence_id"]
        source = self.scratch / "interleaved-source.bin"
        source.write_bytes(b"interleaved writer fixture")
        validated = threading.Event()
        resume = threading.Event()
        writer_errors: list[BaseException] = []
        writer_results: list[dict] = []
        real_inventory_lock = cli_module.inventory_lock
        controlled_once = False

        class PauseAfterValidation:
            def __init__(self, lock: object) -> None:
                self.lock = lock

            def __enter__(self) -> PauseAfterValidation:
                self.lock.acquire()
                return self

            def __exit__(
                self, exc_type: object, exc_value: object, traceback: object
            ) -> None:
                self.lock.release()
                validated.set()
                if not resume.wait(10):
                    raise AssertionError("timed out waiting to resume old writer")

        def controlled_lock(inventory_root: Path) -> object:
            nonlocal controlled_once
            lock = real_inventory_lock(inventory_root)
            if threading.current_thread().name == "old-writer" and not controlled_once:
                controlled_once = True
                return PauseAfterValidation(lock)
            return lock

        def old_writer() -> None:
            try:
                writer_results.append(
                    cli_module.execute(
                        [
                            *common,
                            "--scope",
                            "private",
                            "attach-media",
                            "--evidence-id",
                            evidence_id,
                            "--file",
                            str(source),
                            "--role",
                            "source",
                            "--captured-on",
                            "2026-08-09",
                            "--media-type",
                            "application/octet-stream",
                        ]
                    )
                )
            except BaseException as error:
                writer_errors.append(error)

        with patch.object(cli_module, "inventory_lock", side_effect=controlled_lock):
            writer = threading.Thread(target=old_writer, name="old-writer")
            writer.start()
            self.assertTrue(validated.wait(10))
            rebound = cli_module.execute(
                [
                    "--inventory-root",
                    str(root),
                    "--runtime-dir",
                    str(runtime),
                    "--media-root",
                    str(new_media),
                    "--catalogue-output",
                    str(new_catalogue),
                    "--scope",
                    "private",
                    "runtime-rebind",
                    "--from-runtime",
                    str(runtime),
                    "--from-media-root",
                    str(old_media),
                    "--from-catalogue-output",
                    str(old_catalogue),
                ]
            )
            resume.set()
            writer.join(20)

        self.assertFalse(writer.is_alive())
        self.assertEqual(rebound["status"], "rebound")
        self.assertEqual(writer_results, [])
        self.assertEqual(len(writer_errors), 1)
        self.assertIn("paths changed after this command started", str(writer_errors[0]))
        self.assertFalse((old_media / "sha256").exists())
        self.assertFalse((new_media / "sha256").exists())
        media_rows = (root / "Data" / "store" / "media_assets.jsonl").read_text()
        self.assertEqual(media_rows, "")
        status = cli_module.execute(
            [
                "--inventory-root",
                str(root),
                "--runtime-dir",
                str(runtime),
                "--media-root",
                str(new_media),
                "--catalogue-output",
                str(new_catalogue),
                "status",
            ]
        )
        self.assertEqual(status["verification"]["failures"], [])

    def test_runtime_rebind_refuses_pending_initialization_recovery(self) -> None:
        root = self.scratch / "pending-init-rebind-inventory"
        runtime = self.scratch / "pending-init-rebind-runtime"
        media = self.scratch / "pending-init-rebind-media"
        old_catalogue = self.scratch / "pending-init-old-catalogue.md"
        new_catalogue = self.scratch / "pending-init-new-catalogue.md"
        crashed = subprocess.run(
            [
                sys.executable,
                str(CLI),
                "--inventory-root",
                str(root),
                "--runtime-dir",
                str(runtime),
                "--media-root",
                str(media),
                "--catalogue-output",
                str(old_catalogue),
                "init",
            ],
            env={**os.environ, "PROPERTY_INVENTORY_FAIL_INIT_AFTER_INSTALL": "1"},
            text=True,
            capture_output=True,
            check=False,
        )
        self.assertEqual(crashed.returncode, 83, crashed.stderr or crashed.stdout)
        self.assertTrue((runtime / ".property-inventory-init.json").is_file())
        owner_before = (runtime / ".property-inventory-owner.json").read_bytes()
        binding_before = (root / ".property-inventory-runtime.json").read_bytes()

        rebound = subprocess.run(
            [
                sys.executable,
                str(CLI),
                "--inventory-root",
                str(root),
                "--runtime-dir",
                str(runtime),
                "--media-root",
                str(media),
                "--catalogue-output",
                str(new_catalogue),
                "--scope",
                "private",
                "runtime-rebind",
                "--from-runtime",
                str(runtime),
                "--from-media-root",
                str(media),
                "--from-catalogue-output",
                str(old_catalogue),
            ],
            text=True,
            capture_output=True,
            check=False,
        )
        self.assertNotEqual(rebound.returncode, 0)
        self.assertIn("pending recovery state", rebound.stderr)
        self.assertEqual(
            (runtime / ".property-inventory-owner.json").read_bytes(), owner_before
        )
        self.assertEqual((root / ".property-inventory-runtime.json").read_bytes(), binding_before)
        self.assertFalse(new_catalogue.exists())

    def test_runtime_rebind_refuses_pending_adoption_rollback_without_mutation(
        self,
    ) -> None:
        cli_module = legacy_inventory._CLI
        root = self.scratch / "adoption-rebind-clone"
        shutil.copytree(self.root, root)
        (root / ".property-inventory-runtime.json").unlink()
        runtime = self.scratch / "adoption-rebind-runtime"
        media = self.scratch / "adoption-rebind-media"
        catalogue = self.scratch / "adoption-rebind-catalogue.md"
        command = [
            sys.executable,
            str(CLI),
            "--inventory-root",
            str(root),
            "--runtime-dir",
            str(runtime),
            "--media-root",
            str(media),
            "--catalogue-output",
            str(catalogue),
            "init",
        ]
        crashed = subprocess.run(
            command,
            env={
                **os.environ,
                "PROPERTY_INVENTORY_FAIL_BEFORE_RENDER_REPLACE": "1",
                "PROPERTY_INVENTORY_FAIL_DURING_ADOPTION_ROLLBACK": "runtime",
            },
            text=True,
            capture_output=True,
            check=False,
        )
        self.assertEqual(crashed.returncode, 97, crashed.stderr or crashed.stdout)
        journal = cli_module.adoption_rollback_journal_path(runtime)
        self.assertTrue(journal.is_file())
        self.assertFalse(runtime.exists())
        binding_before = (root / ".property-inventory-runtime.json").read_bytes()

        rebound = subprocess.run(
            [
                sys.executable,
                str(CLI),
                "--inventory-root",
                str(root),
                "--runtime-dir",
                str(runtime),
                "--media-root",
                str(media),
                "--catalogue-output",
                str(catalogue),
                "--scope",
                "private",
                "runtime-rebind",
                "--from-runtime",
                str(runtime),
            ],
            text=True,
            capture_output=True,
            check=False,
        )
        self.assertNotEqual(rebound.returncode, 0)
        self.assertIn("pending recovery state", rebound.stderr)
        self.assertFalse(runtime.exists())
        self.assertEqual((root / ".property-inventory-runtime.json").read_bytes(), binding_before)

        recovered = subprocess.run(
            command, text=True, capture_output=True, check=False
        )
        self.assertEqual(recovered.returncode, 0, recovered.stderr or recovered.stdout)
        self.assertFalse(journal.exists())

    def test_runtime_rebind_refuses_untracked_restore_workspace(self) -> None:
        workspace = self.runtime / ".property-inventory-restore-orphan"
        workspace.mkdir()
        private_bytes = workspace / "private-extraction.bin"
        private_bytes.write_bytes(b"preserve private restore bytes")
        new_catalogue = self.scratch / "orphan-workspace-catalogue.md"
        owner_before = (self.runtime / ".property-inventory-owner.json").read_bytes()
        binding_before = (self.root / ".property-inventory-runtime.json").read_bytes()

        rebound = subprocess.run(
            [
                sys.executable,
                str(CLI),
                "--inventory-root",
                str(self.root),
                "--runtime-dir",
                str(self.runtime),
                "--catalogue-output",
                str(new_catalogue),
                "--scope",
                "private",
                "runtime-rebind",
                "--from-runtime",
                str(self.runtime),
                "--from-catalogue-output",
                str(self.root / "Inventory.md"),
            ],
            text=True,
            capture_output=True,
            check=False,
        )
        self.assertNotEqual(rebound.returncode, 0)
        self.assertIn("pending recovery state", rebound.stderr)
        self.assertEqual(private_bytes.read_bytes(), b"preserve private restore bytes")
        self.assertEqual(
            (self.runtime / ".property-inventory-owner.json").read_bytes(), owner_before
        )
        self.assertEqual(
            (self.root / ".property-inventory-runtime.json").read_bytes(), binding_before
        )
        self.assertFalse(new_catalogue.exists())

    def test_runtime_rebind_upgrades_legacy_binding_without_owner(self) -> None:
        new_runtime = self.scratch / "legacy-rebind-new-runtime"
        old_catalogue = self.root / "Inventory.md"
        new_catalogue = self.scratch / "legacy-rebind-new-catalogue.md"
        binding_path = self.root / ".property-inventory-runtime.json"
        binding_path.write_text(
            json.dumps({"format": 1, "runtime_dir": str(self.runtime.resolve())}) + "\n"
        )
        (self.runtime / ".property-inventory-owner.json").unlink()
        rebound = subprocess.run(
            [
                sys.executable,
                str(CLI),
                "--inventory-root",
                str(self.root),
                "--runtime-dir",
                str(new_runtime),
                "--catalogue-output",
                str(new_catalogue),
                "--scope",
                "private",
                "runtime-rebind",
                "--from-runtime",
                str(self.runtime),
                "--from-catalogue-output",
                str(old_catalogue),
            ],
            text=True,
            capture_output=True,
            check=False,
        )
        self.assertEqual(rebound.returncode, 0, rebound.stderr or rebound.stdout)
        binding = json.loads(binding_path.read_text())
        self.assertEqual(binding["format"], 2)
        self.assertEqual(binding["runtime_dir"], str(new_runtime.resolve()))
        owner = json.loads((new_runtime / ".property-inventory-owner.json").read_text())
        self.assertEqual(owner["runtime_dir"], str(new_runtime.resolve()))
        status = subprocess.run(
            [
                sys.executable,
                str(CLI),
                "--inventory-root",
                str(self.root),
                "--runtime-dir",
                str(new_runtime),
                "--catalogue-output",
                str(new_catalogue),
                "status",
            ],
            text=True,
            capture_output=True,
            check=False,
        )
        self.assertEqual(status.returncode, 0, status.stderr or status.stdout)

    def test_runtime_rebind_upgrades_legacy_binding_after_path_rename(self) -> None:
        old_catalogue = self.root / "Inventory.md"
        new_catalogue = self.scratch / "legacy-renamed-catalogue.md"
        binding_path = self.root / ".property-inventory-runtime.json"
        binding_path.write_text(
            json.dumps({"format": 1, "runtime_dir": str(self.runtime.resolve())}) + "\n"
        )
        owner_path = self.runtime / ".property-inventory-owner.json"
        owner = json.loads(owner_path.read_text())
        owner["installation_id"] = legacy_inventory._CLI.legacy_installation_id(
            self.root
        )
        owner_path.write_text(json.dumps(owner) + "\n")
        rebound = subprocess.run(
            [
                sys.executable,
                str(CLI),
                "--inventory-root",
                str(self.root),
                "--runtime-dir",
                str(self.runtime),
                "--catalogue-output",
                str(new_catalogue),
                "--scope",
                "private",
                "runtime-rebind",
                "--from-runtime",
                str(self.runtime),
                "--from-catalogue-output",
                str(old_catalogue),
            ],
            text=True,
            capture_output=True,
            check=False,
        )
        self.assertEqual(rebound.returncode, 0, rebound.stderr or rebound.stdout)
        self.assertEqual(json.loads(binding_path.read_text())["format"], 2)
        self.assertEqual(
            json.loads(owner_path.read_text())["catalogue_output"],
            str(new_catalogue.resolve()),
        )
        status = subprocess.run(
            [
                sys.executable,
                str(CLI),
                "--inventory-root",
                str(self.root),
                "--runtime-dir",
                str(self.runtime),
                "--catalogue-output",
                str(new_catalogue),
                "status",
            ],
            text=True,
            capture_output=True,
            check=False,
        )
        self.assertEqual(status.returncode, 0, status.stderr or status.stdout)

    def test_interrupted_multi_file_commit_recovers_on_next_command(self) -> None:
        for fail_after in range(1, 6):
            with self.subTest(fail_after=fail_after):
                name = f"acceptance-crash-object-{fail_after}"
                environment = dict(os.environ)
                environment["PROPERTY_INVENTORY_FAIL_AFTER_REPLACE"] = str(fail_after)
                interrupted = subprocess.run(
                    self.command(
                        "order",
                        "--actor",
                        "Crash fixture",
                        "--source-ref",
                        "Crash fixture order",
                        "--name",
                        name,
                        "--category",
                        "test fixture",
                        "--ordered-on",
                        "2026-08-05",
                        "--order-placed",
                    ),
                    text=True,
                    capture_output=True,
                    check=False,
                    env=environment,
                )
                self.assertEqual(interrupted.returncode, 97)
                recovered = self.cli("status")
                expected_recovery = "completed" if fail_after == 5 else "rolled_back"
                self.assertEqual(recovered["recovery"], expected_recovery)
                self.assertEqual(
                    self.cli("search", name)["recorded"],
                    fail_after == 5,
                )
                self.assertFalse(
                    (self.runtime / ".property-inventory-transaction.json").exists()
                )
                self.assertFalse(
                    (self.runtime / ".property-inventory-transaction").exists()
                )

    def test_terminal_states_cannot_be_implicitly_resurrected(self) -> None:
        self.cli(
            "add-location", "--name", "Lifecycle fixture location", "--location-id", "loc-lifecycle",
            "--kind", "room",
        )
        cancelled = self.cli(
            "order",
            "--actor",
            "Lifecycle fixture",
            "--source-ref",
            "Cancelled order fixture",
            "--name",
            "acceptance-cancelled-object",
            "--category",
            "test fixture",
            "--ordered-on",
            "2026-08-05",
            "--order-placed",
        )["result"]["item_id"]
        self.cli(
            "change",
            "--actor",
            "Lifecycle fixture",
            "--source-ref",
            "Cancellation fixture",
            "--item-id",
            cancelled,
            "--event-type",
            "cancelled",
            "--occurred-on",
            "2026-08-05",
        )
        receive_error = self.cli_fails(
            "receive",
            "--actor",
            "Lifecycle fixture",
            "--source-ref",
            "Invalid delivery fixture",
            "--item-id",
            cancelled,
            "--received-on",
            "2026-08-06",
        )
        self.assertIn("cannot receive item in state refunded", receive_error["error"])
        reorder_error = self.cli_fails(
            "order",
            "--actor",
            "Lifecycle fixture",
            "--source-ref",
            "Invalid reorder fixture",
            "--name",
            "acceptance-cancelled-object",
            "--category",
            "test fixture",
            "--ordered-on",
            "2026-08-06",
            "--order-placed",
            "--existing-item-id",
            cancelled,
        )
        self.assertIn("only for an unverified candidate", reorder_error["error"])
        replan_error = self.cli_fails(
            "plan",
            "--actor",
            "Lifecycle fixture",
            "--source-ref",
            "Invalid replan fixture",
            "--name",
            "acceptance-cancelled-object",
            "--category",
            "test fixture",
            "--planned-on",
            "2026-08-06",
            "--existing-item-id",
            cancelled,
        )
        self.assertIn("must be planned or unknown", replan_error["error"])
        quantity_error = self.cli_fails(
            "change",
            "--actor",
            "Lifecycle fixture",
            "--source-ref",
            "Invalid candidate quantity fixture",
            "--item-id",
            self.cli(
                "order",
                "--actor",
                "Lifecycle fixture",
                "--source-ref",
                "Candidate quantity order fixture",
                "--name",
                "acceptance-candidate-quantity-object",
                "--category",
                "test fixture",
                "--ordered-on",
                "2026-08-06",
                "--order-placed",
            )["result"]["item_id"],
            "--event-type",
            "quantity_changed",
            "--occurred-on",
            "2026-08-06",
            "--quantity",
            "2",
        )
        self.assertIn("state candidate", quantity_error["error"])

        disposed = self.cli(
            "order",
            "--actor",
            "Lifecycle fixture",
            "--source-ref",
            "Disposed order fixture",
            "--name",
            "acceptance-disposed-object",
            "--category",
            "test fixture",
            "--ordered-on",
            "2026-08-05",
            "--order-placed",
        )["result"]["item_id"]
        self.cli(
            "receive",
            "--actor",
            "Lifecycle fixture",
            "--source-ref",
            "Disposed delivery fixture",
            "--item-id",
            disposed,
            "--received-on",
            "2026-08-06",
            "--location-id",
            "loc-lifecycle",
        )
        self.cli(
            "change",
            "--actor",
            "Lifecycle fixture",
            "--source-ref",
            "Disposal fixture",
            "--item-id",
            disposed,
            "--event-type",
            "disposed",
            "--occurred-on",
            "2026-08-07",
        )
        lend_error = self.cli_fails(
            "change",
            "--actor",
            "Lifecycle fixture",
            "--source-ref",
            "Invalid lending fixture",
            "--item-id",
            disposed,
            "--event-type",
            "lent",
            "--occurred-on",
            "2026-08-08",
        )
        self.assertIn("cannot apply lent to item in state disposed", lend_error["error"])

    def test_prepare_and_post_verify_crashes_recover_deterministically(self) -> None:
        cases = (
            ("PROPERTY_INVENTORY_FAIL_AFTER_PREPARE", 96, "rolled_back", False),
            ("PROPERTY_INVENTORY_FAIL_AFTER_VERIFY", 98, "completed", True),
        )
        for variable, exit_code, recovery, recorded in cases:
            with self.subTest(variable=variable):
                name = f"acceptance-{variable.casefold().replace('_', '-')}"
                environment = dict(os.environ)
                environment[variable] = "1"
                interrupted = subprocess.run(
                    self.command(
                        "order",
                        "--actor",
                        "Crash fixture",
                        "--source-ref",
                        "Crash boundary fixture",
                        "--name",
                        name,
                        "--category",
                        "test fixture",
                        "--ordered-on",
                        "2026-08-05",
                        "--order-placed",
                    ),
                    text=True,
                    capture_output=True,
                    check=False,
                    env=environment,
                )
                self.assertEqual(interrupted.returncode, exit_code)
                self.assertEqual(self.cli("status")["recovery"], recovery)
                self.assertEqual(self.cli("search", name)["recorded"], recorded)

    def test_recovery_refuses_to_overwrite_an_unexpected_edit(self) -> None:
        environment = dict(os.environ)
        environment["PROPERTY_INVENTORY_FAIL_AFTER_REPLACE"] = "1"
        interrupted = subprocess.run(
            self.command(
                "order",
                "--actor",
                "Crash fixture",
                "--source-ref",
                "Unexpected edit fixture",
                "--name",
                "acceptance-unexpected-edit-object",
                "--category",
                "test fixture",
                "--ordered-on",
                "2026-08-05",
                "--order-placed",
            ),
            text=True,
            capture_output=True,
            check=False,
            env=environment,
        )
        self.assertEqual(interrupted.returncode, 97)
        items = self.store / "items.jsonl"
        items.write_bytes(items.read_bytes() + b" \n")
        unexpected = items.read_bytes()
        failure = self.cli_fails("status")
        self.assertIn("changed outside the pending transaction: items.jsonl", failure["error"])
        self.assertEqual(items.read_bytes(), unexpected)

    def test_recovery_rejects_cross_inconsistent_journal_before_mutation(self) -> None:
        before = {
            path.name: path.read_bytes() for path in sorted(self.store.glob("*.jsonl"))
        }
        environment = dict(os.environ)
        environment["PROPERTY_INVENTORY_FAIL_AFTER_PREPARE"] = "1"
        crashed = subprocess.run(
            self.command(
                "order",
                "--actor",
                "Journal integrity fixture",
                "--source-ref",
                "Journal integrity fixture",
                "--name",
                "journal-integrity-object",
                "--category",
                "test fixture",
                "--ordered-on",
                "2026-08-05",
                "--order-placed",
            ),
            text=True,
            capture_output=True,
            check=False,
            env=environment,
        )
        self.assertEqual(crashed.returncode, 96, crashed.stderr or crashed.stdout)
        journal_path = self.runtime / ".property-inventory-transaction.json"
        journal = json.loads(journal_path.read_text())
        changed_record = next(
            record for record in journal["files"] if record["old_sha256"] is not None
        )
        changed_record["old_sha256"] = None
        journal_path.write_text(json.dumps(journal, sort_keys=True) + "\n")

        failed = self.cli_fails("status")

        self.assertIn("hashes disagree", failed["error"])
        self.assertEqual(
            {path.name: path.read_bytes() for path in sorted(self.store.glob("*.jsonl"))},
            before,
        )
        self.assertTrue(journal_path.exists())

    def test_missing_recovery_workspace_requires_a_complete_generation(self) -> None:
        for variable, exit_code, expected in (
            ("PROPERTY_INVENTORY_FAIL_AFTER_PREPARE", 96, "rolled_back"),
            ("PROPERTY_INVENTORY_FAIL_AFTER_VERIFY", 98, "completed"),
        ):
            with self.subTest(variable=variable):
                name = f"acceptance-missing-workspace-{exit_code}"
                environment = dict(os.environ)
                environment[variable] = "1"
                interrupted = subprocess.run(
                    self.command(
                        "order",
                        "--actor",
                        "Crash fixture",
                        "--source-ref",
                        "Missing workspace fixture",
                        "--name",
                        name,
                        "--category",
                        "test fixture",
                        "--ordered-on",
                        "2026-08-05",
                        "--order-placed",
                    ),
                    text=True,
                    capture_output=True,
                    check=False,
                    env=environment,
                )
                self.assertEqual(interrupted.returncode, exit_code)
                shutil.rmtree(self.runtime / ".property-inventory-transaction")
                self.assertEqual(self.cli("status")["recovery"], expected)

        environment = dict(os.environ)
        environment["PROPERTY_INVENTORY_FAIL_AFTER_REPLACE"] = "1"
        interrupted = subprocess.run(
            self.command(
                "order",
                "--actor",
                "Crash fixture",
                "--source-ref",
                "Missing mixed workspace fixture",
                "--name",
                "acceptance-missing-mixed-workspace",
                "--category",
                "test fixture",
                "--ordered-on",
                "2026-08-05",
                "--order-placed",
            ),
            text=True,
            capture_output=True,
            check=False,
            env=environment,
        )
        self.assertEqual(interrupted.returncode, 97)
        shutil.rmtree(self.runtime / ".property-inventory-transaction")
        failure = self.cli_fails("status")
        self.assertIn("workspace is missing while the canonical store is mixed", failure["error"])

    def test_lock_is_shared_across_runtime_directories(self) -> None:
        alternate_runtime = self.scratch / "alternate-runtime"
        command = [
            sys.executable,
            str(CLI),
            "--inventory-root",
            str(self.root),
            "--runtime-dir",
            str(alternate_runtime),
            "add-location",
            "--name",
            "Blocked room",
            "--kind",
            "room",
        ]
        with FileLock(canonical_lock_path(self.root)):
            blocked = subprocess.run(
                command, text=True, capture_output=True, check=False
            )
        self.assertNotEqual(blocked.returncode, 0)
        self.assertIn("another inventory writer", blocked.stderr)

    def test_runtime_binding_prevents_bypassing_pending_recovery(self) -> None:
        environment = dict(os.environ)
        environment["PROPERTY_INVENTORY_FAIL_AFTER_REPLACE"] = "1"
        interrupted = subprocess.run(
            self.command(
                "order",
                "--actor",
                "Runtime binding fixture",
                "--source-ref",
                "Runtime binding fixture",
                "--name",
                "runtime-binding-object",
                "--category",
                "test fixture",
                "--ordered-on",
                "2026-08-05",
                "--order-placed",
            ),
            text=True,
            capture_output=True,
            check=False,
            env=environment,
        )
        self.assertEqual(interrupted.returncode, 97)

        alternate = subprocess.run(
            [
                sys.executable,
                str(CLI),
                "--inventory-root",
                str(self.root),
                "--runtime-dir",
                str(self.scratch / "different-runtime"),
                "status",
            ],
            text=True,
            capture_output=True,
            check=False,
        )
        self.assertNotEqual(alternate.returncode, 0)
        self.assertIn("bound to a different runtime", alternate.stderr)

        recovered = self.cli("status")
        self.assertEqual(recovered["recovery"], "rolled_back")


class WriterGateTest(unittest.TestCase):
    def test_dirty_store_requires_explicit_same_batch_continuation(self) -> None:
        with tempfile.TemporaryDirectory(prefix="inventory-writer-gate-") as temp_name:
            root = Path(temp_name)
            data = root / "Data"
            store = data / "store"
            store.mkdir(parents=True)
            row = store / "items.jsonl"
            row.write_text('{"item_id":"baseline"}\n')
            subprocess.run(["git", "init", "-q", "-b", "main"], cwd=root, check=True)
            subprocess.run(["git", "config", "user.name", "Test"], cwd=root, check=True)
            subprocess.run(["git", "config", "user.email", "test@x"], cwd=root, check=True)
            subprocess.run(["git", "add", "Data/store/items.jsonl"], cwd=root, check=True)
            subprocess.run(["git", "commit", "-qm", "baseline"], cwd=root, check=True)

            git_store_is_clean(data, continue_batch=False)
            row.write_text('{"item_id":"same-batch-change"}\n')
            with self.assertRaises(InventoryError):
                git_store_is_clean(data, continue_batch=False)
            git_store_is_clean(data, continue_batch=True)


class TransactionPrimitiveTest(unittest.TestCase):
    def paths(self, root: Path) -> dict[str, Path]:
        data = root / "Data"
        runtime = root / "runtime"
        return {
            "data": data,
            "store": data / "store",
            "runtime": runtime,
            "transaction_journal": runtime / ".property-inventory-transaction.json",
            "transaction_workspace": runtime / ".property-inventory-transaction",
        }

    def test_new_canonical_file_is_removed_on_rollback(self) -> None:
        with tempfile.TemporaryDirectory(prefix="inventory-new-file-rollback-") as temp_name:
            root = Path(temp_name)
            paths = self.paths(root)
            paths["store"].mkdir(parents=True)
            staged = root / "staged"
            staged.mkdir()
            (staged / "items.jsonl").write_text('{"item_id":"new"}\n')
            journal = legacy_inventory._CLI.prepare_transaction(
                paths, staged, ["items.jsonl"]
            )
            self.assertIsNone(journal["files"][0]["old_sha256"])
            legacy_inventory._CLI.replace_prepared_store(paths, journal)
            self.assertTrue((paths["store"] / "items.jsonl").exists())
            legacy_inventory._CLI.restore_prepared_store(paths, journal)
            self.assertFalse((paths["store"] / "items.jsonl").exists())
            legacy_inventory._CLI.remove_transaction_state(paths)

    def test_corrupt_prepared_file_cannot_commit(self) -> None:
        with tempfile.TemporaryDirectory(prefix="inventory-prepared-corruption-") as temp_name:
            root = Path(temp_name)
            paths = self.paths(root)
            paths["store"].mkdir(parents=True)
            live = paths["store"] / "items.jsonl"
            live.write_text('{"item_id":"old"}\n')
            staged = root / "staged"
            staged.mkdir()
            (staged / "items.jsonl").write_text('{"item_id":"intended"}\n')
            journal = legacy_inventory._CLI.prepare_transaction(
                paths, staged, ["items.jsonl"]
            )
            prepared = paths["transaction_workspace"] / "new" / "items.jsonl"
            prepared.write_text('{"item_id":"different-but-valid"}\n')
            with self.assertRaisesRegex(InventoryError, "missing or corrupt: items.jsonl"):
                legacy_inventory._CLI.replace_prepared_store(paths, journal)
            self.assertEqual(live.read_text(), '{"item_id":"old"}\n')
            legacy_inventory._CLI.remove_transaction_state(paths)


if __name__ == "__main__":
    unittest.main()
