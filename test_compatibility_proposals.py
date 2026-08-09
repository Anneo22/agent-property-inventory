"""Acceptance tests for typed compatibility and atomic proposals."""

from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

HERE = Path(__file__).resolve().parent
CLI = HERE / "property_inventory.py"


class CompatibilityProposalTest(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory(prefix="inventory-proposals-")
        self.scratch = Path(self.temp.name)
        self.root = self.scratch / "inventory"
        self.runtime = self.scratch / "runtime"
        self.cli("init")

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

    def run_cli(self, *arguments: str) -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            self.command(*arguments), text=True, capture_output=True, check=False
        )

    def cli(self, *arguments: str) -> dict:
        completed = self.run_cli(*arguments)
        self.assertEqual(completed.returncode, 0, completed.stderr or completed.stdout)
        return json.loads(completed.stdout)

    def cli_fails(self, *arguments: str) -> dict:
        completed = self.run_cli(*arguments)
        self.assertNotEqual(completed.returncode, 0, completed.stdout)
        return json.loads(completed.stderr)

    def order(
        self,
        name: str,
        *,
        interface: str | None = None,
        sensitivity: str = "personal",
    ) -> dict:
        arguments = [
            "order",
            "--actor",
            "Test agent",
            "--source-ref",
            f"Order evidence for {name}",
            "--name",
            name,
            "--category",
            "test fixture",
            "--ordered-on",
            "2026-08-05",
            "--order-placed",
            "--sensitivity",
            sensitivity,
        ]
        if interface is not None:
            arguments.extend(("--interface", interface))
        return self.cli(*arguments)["result"]

    def snapshot(self) -> dict[str, bytes]:
        return {path.name: path.read_bytes() for path in sorted(self.store.glob("*.jsonl"))}

    def write_operations(self, name: str, operations: list[list[str]]) -> Path:
        path = self.scratch / name
        path.write_text(json.dumps(operations))
        return path

    def test_normalized_evidence_proves_compatibility_but_legacy_text_does_not(self) -> None:
        driver = self.order("test driver")
        bit = self.order("test bit")
        legacy = self.order("legacy-only bit", interface="1/4 inch hex")

        driver_model = self.cli("show", driver["item_id"])["item"]["model_id"]
        bit_model = self.cli("show", bit["item_id"])["item"]["model_id"]
        self.cli(
            "add-interface",
            "--model-id",
            driver_model,
            "--evidence-id",
            driver["evidence_id"],
            "--family",
            "hex drive",
            "--standard",
            "1/4 inch",
            "--direction",
            "socket",
            "--role",
            "accepts",
        )
        self.cli(
            "add-interface",
            "--model-id",
            bit_model,
            "--evidence-id",
            bit["evidence_id"],
            "--family",
            "hex drive",
            "--standard",
            "1/4 inch",
            "--direction",
            "plug",
            "--role",
            "provides",
        )

        compatible = self.cli("compatibility", driver["item_id"], bit["item_id"])
        self.assertEqual(compatible["outcome"], "compatible")
        self.assertEqual(
            compatible["evidence_ids"],
            sorted((driver["evidence_id"], bit["evidence_id"])),
        )

        unknown = self.cli("compatibility", driver["item_id"], legacy["item_id"])
        self.assertEqual(unknown["outcome"], "unknown")
        self.assertIn("legacy text", unknown["reason"])

        self.assertIn(
            "two different items",
            self.cli_fails(
                "compatibility", driver["item_id"], driver["item_id"]
            )["error"],
        )

    def test_explicit_incompatibility_overrides_normalized_interface_match(self) -> None:
        first = self.order("explicit first")
        second = self.order("explicit second")
        self.cli(
            "relate",
            "--subject-item-id",
            first["item_id"],
            "--object-item-id",
            second["item_id"],
            "--predicate",
            "not_compatible",
            "--confidence",
            "verified",
            "--captured-on",
            "2026-08-05",
            "--evidence-type",
            "research",
            "--claim-strength",
            "research_only",
            "--source-ref",
            "Explicit incompatibility fixture",
        )
        result = self.cli("compatibility", first["item_id"], second["item_id"])
        self.assertEqual(result["outcome"], "incompatible")
        self.assertEqual(len(result["evidence_ids"]), 1)

    def test_uncertain_relationship_never_becomes_a_definitive_answer(self) -> None:
        first = self.order("uncertain first")
        second = self.order("uncertain second")
        self.cli(
            "relate",
            "--subject-item-id",
            first["item_id"],
            "--object-item-id",
            second["item_id"],
            "--predicate",
            "works_with",
            "--confidence",
            "unknown",
            "--captured-on",
            "2026-08-05",
            "--evidence-type",
            "research",
            "--claim-strength",
            "research_only",
            "--source-ref",
            "Uncertain compatibility fixture",
        )
        result = self.cli("compatibility", first["item_id"], second["item_id"])
        self.assertEqual(result["outcome"], "unknown")
        self.assertIn("insufficient confidence", result["reason"])

    def test_interface_evidence_must_support_the_selected_model(self) -> None:
        selected = self.order("selected model")
        unrelated = self.order("unrelated evidence owner")
        selected_model = self.cli("show", selected["item_id"])["item"]["model_id"]
        failed = self.cli_fails(
            "add-interface",
            "--model-id",
            selected_model,
            "--evidence-id",
            unrelated["evidence_id"],
            "--family",
            "USB",
            "--standard",
            "USB-C",
            "--direction",
            "socket",
            "--role",
            "accepts",
        )
        self.assertIn("must already support an item", failed["error"])

    def test_matching_broad_interface_families_remain_unknown(self) -> None:
        first = self.order("broad interface first")
        second = self.order("broad interface second")
        for record, direction, role in (
            (first, "socket", "accepts"),
            (second, "plug", "provides"),
        ):
            model_id = self.cli("show", record["item_id"])["item"]["model_id"]
            self.cli(
                "add-interface",
                "--model-id",
                model_id,
                "--evidence-id",
                record["evidence_id"],
                "--family",
                "USB",
                "--direction",
                direction,
                "--role",
                role,
                "--properties",
                '{"colour":"black"}',
            )
        result = self.cli("compatibility", first["item_id"], second["item_id"])
        self.assertEqual(result["outcome"], "unknown")

    def test_compatibility_never_uses_evidence_hidden_from_the_scope(self) -> None:
        first = self.order("scope-safe first", sensitivity="low")
        second = self.order("scope-safe second", sensitivity="low")
        started = self.cli(
            "--scope",
            "private",
            "maintenance-start",
            "--performed-on",
            "2026-08-06",
            "--activity",
            "High-scope compatibility check",
            "--source-ref",
            "Private compatibility fixture",
            "--evidence-type",
            "research",
            "--sensitivity",
            "high",
        )
        finished = self.cli(
            "--scope",
            "private",
            "maintenance-finish",
            started["maintenance_session_id"],
            "--elapsed-seconds",
            "1",
            "--correction-count",
            "0",
            "--review-count",
            "1",
            "--item-id",
            first["item_id"],
            "--item-id",
            second["item_id"],
        )["result"]
        for record, direction, role in (
            (first, "socket", "accepts"),
            (second, "plug", "provides"),
        ):
            model_id = self.cli("show", record["item_id"])["item"]["model_id"]
            self.cli(
                "add-interface",
                "--model-id",
                model_id,
                "--evidence-id",
                finished["evidence_id"],
                "--family",
                "hex drive",
                "--standard",
                "1/4 inch",
                "--direction",
                direction,
                "--role",
                role,
            )
        private = self.cli("compatibility", first["item_id"], second["item_id"])
        self.assertEqual(private["outcome"], "compatible")
        public = self.cli(
            "--scope", "public", "compatibility", first["item_id"], second["item_id"]
        )
        self.assertEqual(public["outcome"], "unknown")
        self.assertEqual(public["evidence_ids"], [])
        self.assertNotIn("match", public["reason"])

    def test_failed_proposal_leaves_every_canonical_byte_unchanged(self) -> None:
        operations = self.write_operations(
            "invalid-proposal.json",
            [
                [
                    "add-location",
                    "--name",
                    "Must roll back",
                    "--location-id",
                    "loc-must-roll-back",
                    "--kind",
                    "room",
                ],
                [
                    "add-location",
                    "--name",
                    "Invalid child",
                    "--parent-location-id",
                    "loc-does-not-exist",
                    "--kind",
                    "room",
                ],
            ],
        )
        prepared = self.cli("propose", "--operations", str(operations))
        before = self.snapshot()
        failed = self.cli_fails(
            "proposal-apply", prepared["proposal"]["proposal_id"]
        )
        self.assertIn("proposal operation 2 failed", failed["error"])
        self.assertEqual(self.snapshot(), before)
        shown = self.cli(
            "proposal-show", prepared["proposal"]["proposal_id"]
        )
        self.assertEqual(shown["proposal"]["status"], "prepared")

    def test_failed_proposal_cannot_modify_inherited_external_catalogue(self) -> None:
        operations = self.write_operations(
            "external-catalogue-proposal.json",
            [
                [
                    "add-location",
                    "--name",
                    "Sandbox-only location",
                    "--location-id",
                    "loc-sandbox-only",
                    "--kind",
                    "room",
                ],
                [
                    "add-location",
                    "--name",
                    "Invalid sandbox child",
                    "--parent-location-id",
                    "loc-does-not-exist",
                    "--kind",
                    "room",
                ],
            ],
        )
        prepared = self.cli("propose", "--operations", str(operations))
        external_catalogue = self.scratch / "external" / "Inventory.md"
        external_catalogue.parent.mkdir()
        external_catalogue.write_bytes(b"external catalogue must remain unchanged\n")
        before_store = self.snapshot()
        before_catalogue = external_catalogue.read_bytes()
        environment = dict(os.environ)
        environment["PROPERTY_INVENTORY_CATALOGUE_OUTPUT"] = str(external_catalogue)
        environment["PROPERTY_INVENTORY_CATALOGUE_SCOPE"] = "public"

        failed_process = subprocess.run(
            self.command("proposal-apply", prepared["proposal"]["proposal_id"]),
            text=True,
            capture_output=True,
            check=False,
            env=environment,
        )

        self.assertNotEqual(failed_process.returncode, 0, failed_process.stdout)
        failed = json.loads(failed_process.stderr)
        self.assertIn("owned by a different inventory instance", failed["error"])
        self.assertEqual(self.snapshot(), before_store)
        self.assertEqual(external_catalogue.read_bytes(), before_catalogue)

    def test_successful_proposal_is_one_verified_commit_and_cannot_reapply(self) -> None:
        operations = self.write_operations(
            "valid-proposal.json",
            [
                [
                    "add-location",
                    "--name",
                    "Proposal parent",
                    "--location-id",
                    "loc-proposal-parent",
                    "--kind",
                    "place",
                ],
                [
                    "add-location",
                    "--name",
                    "Proposal child",
                    "--location-id",
                    "loc-proposal-child",
                    "--parent-location-id",
                    "loc-proposal-parent",
                    "--kind",
                    "room",
                ],
            ],
        )
        prepared = self.cli("propose", "--operations", str(operations))
        backups_before = len(list((self.runtime / "backups").glob("*")))
        applied = self.cli("proposal-apply", prepared["proposal"]["proposal_id"])
        self.assertEqual(applied["status"], "committed_to_store")
        self.assertEqual(applied["checks"]["verification"]["failures"], [])
        self.assertEqual(
            len(list((self.runtime / "backups").glob("*"))), backups_before + 1
        )
        locations = [json.loads(line) for line in (self.store / "locations.jsonl").read_text().splitlines()]
        self.assertEqual(
            next(row for row in locations if row["location_id"] == "loc-proposal-child")[
                "parent_location_id"
            ],
            "loc-proposal-parent",
        )
        self.assertIn(
            "not prepared",
            self.cli_fails(
                "proposal-apply", prepared["proposal"]["proposal_id"]
            )["error"],
        )

    def test_proposal_sandbox_preserves_declared_verification_context(self) -> None:
        item = self.order("grandfathered policy fixture")
        items_path = self.store / "items.jsonl"
        items = [json.loads(line) for line in items_path.read_text().splitlines()]
        for row in items:
            if row["item_id"] == item["item_id"]:
                row["ownership_state"] = "planned"
        items_path.write_text(
            "".join(json.dumps(row, sort_keys=True) + "\n" for row in items)
        )
        events_path = self.store / "inventory_events.jsonl"
        events = [json.loads(line) for line in events_path.read_text().splitlines()]
        for row in events:
            if row["item_id"] == item["item_id"]:
                row["event_type"] = "ingested"
        events_path.write_text(
            "".join(json.dumps(row, sort_keys=True) + "\n" for row in events)
        )
        policy = self.root / "Data" / "verification_policy.json"
        policy.write_text(
            json.dumps({"state_overrides": {item["item_id"]: "planned"}}) + "\n"
        )
        self.cli("auxiliary-manifest", "--include", "verification_policy.json")
        self.assertEqual(self.cli("status")["verification"]["failures"], [])

        operations = self.write_operations(
            "auxiliary-context-proposal.json",
            [["add-location", "--name", "Policy-aware proposal", "--kind", "room"]],
        )
        prepared = self.cli("propose", "--operations", str(operations))
        applied = self.cli("proposal-apply", prepared["proposal"]["proposal_id"])

        self.assertEqual(applied["checks"]["verification"]["failures"], [])
        self.assertEqual(applied["result"]["operations"][0]["checks"]["auxiliary_files"], 2)

    def test_stale_proposal_fails_without_touching_current_store(self) -> None:
        operations = self.write_operations(
            "stale-proposal.json",
            [["add-location", "--name", "Stale", "--kind", "room"]],
        )
        prepared = self.cli("propose", "--operations", str(operations))
        self.cli(
            "add-location",
            "--name",
            "Intervening change",
            "--location-id",
            "loc-intervening",
            "--kind",
            "room",
        )
        before = self.snapshot()
        failed = self.cli_fails(
            "proposal-apply", prepared["proposal"]["proposal_id"]
        )
        self.assertIn("proposal is stale", failed["error"])
        self.assertEqual(self.snapshot(), before)

    def test_retry_recovers_applied_status_after_post_commit_process_death(self) -> None:
        for command in (
            ("git", "init", "-q"),
            ("git", "config", "user.email", "inventory-test"),
            ("git", "config", "user.name", "Inventory Test"),
            ("git", "add", "."),
            ("git", "commit", "-qm", "fixture"),
        ):
            completed = subprocess.run(
                command,
                cwd=self.root,
                text=True,
                capture_output=True,
                check=False,
            )
            self.assertEqual(completed.returncode, 0, completed.stderr)
        operations = self.write_operations(
            "crash-proposal.json",
            [
                [
                    "add-location",
                    "--name",
                    "Committed before crash",
                    "--location-id",
                    "loc-committed-before-crash",
                    "--kind",
                    "room",
                ]
            ],
        )
        prepared = self.cli("propose", "--operations", str(operations))
        proposal_id = prepared["proposal"]["proposal_id"]
        environment = dict(os.environ)
        environment["PROPERTY_INVENTORY_FAIL_PROPOSAL_AFTER_COMMIT"] = "1"
        crashed = subprocess.run(
            self.command("proposal-apply", proposal_id),
            text=True,
            capture_output=True,
            check=False,
            env=environment,
        )
        self.assertEqual(crashed.returncode, 99, crashed.stderr or crashed.stdout)
        self.assertEqual(
            self.cli("proposal-show", proposal_id)["proposal"]["status"], "prepared"
        )
        dirty = subprocess.run(
            ("git", "status", "--porcelain", "--", "Data/store"),
            cwd=self.root,
            text=True,
            capture_output=True,
            check=False,
        )
        self.assertIn("proposal_commits.jsonl", dirty.stdout)

        recovered = self.cli("proposal-apply", proposal_id)
        self.assertEqual(recovered["status"], "recovered_applied")
        self.assertTrue(recovered["result"]["recovered_as_already_applied"])
        self.assertEqual(
            self.cli("proposal-show", proposal_id)["proposal"]["status"], "applied"
        )
        receipts = [
            json.loads(line)
            for line in (self.store / "proposal_commits.jsonl").read_text().splitlines()
        ]
        self.assertEqual([row["proposal_id"] for row in receipts], [proposal_id])


if __name__ == "__main__":
    unittest.main()
