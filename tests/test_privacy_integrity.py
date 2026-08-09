"""Adversarial privacy and tamper-integrity contracts for the public CLI surface."""

from __future__ import annotations

import json
import stat
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

HERE = Path(__file__).resolve().parents[1]
CLI = HERE / "property_inventory.py"
CANARY = "PRIVATE-CANARY-MUST-NOT-LEAK"


class PrivacyIntegrityTests(unittest.TestCase):
    """Prove private inventory state neither leaks nor serves after tampering."""

    def setUp(self) -> None:
        self.temporary_directory = tempfile.TemporaryDirectory(
            prefix="inventory-privacy-integrity-"
        )
        self.scratch = Path(self.temporary_directory.name)
        self.root = self.scratch / "inventory"
        self.runtime = self.scratch / "runtime"
        self.media = self.scratch / "media"
        self.catalogue = self.scratch / "catalogue" / "Inventory.md"

    def tearDown(self) -> None:
        self.temporary_directory.cleanup()

    def command(self, *arguments: str, scope: str = "private") -> list[str]:
        return [
            sys.executable,
            str(CLI),
            "--inventory-root",
            str(self.root),
            "--runtime-dir",
            str(self.runtime),
            "--media-root",
            str(self.media),
            "--catalogue-output",
            str(self.catalogue),
            "--scope",
            scope,
            *arguments,
        ]

    def execute(self, *arguments: str, scope: str = "private", succeeds: bool = True) -> dict:
        completed = subprocess.run(
            self.command(*arguments, scope=scope),
            text=True,
            capture_output=True,
            check=False,
        )
        if succeeds:
            self.assertEqual(completed.returncode, 0, completed.stderr or completed.stdout)
            return json.loads(completed.stdout)
        self.assertNotEqual(completed.returncode, 0, completed.stdout)
        return json.loads(completed.stderr)

    def initialize(self) -> None:
        initialized = self.execute("init")
        self.assertEqual(initialized["status"], "initialized")

    def assert_mode(self, path: Path, expected: int) -> None:
        self.assertTrue(path.exists(), f"expected private product path to exist: {path}")
        self.assertEqual(
            stat.S_IMODE(path.stat().st_mode),
            expected,
            f"unexpected mode for {path}",
        )

    def assert_private_tree(self, root: Path) -> None:
        self.assert_mode(root, 0o700)
        for path in sorted(root.rglob("*")):
            self.assertFalse(path.is_symlink(), f"private product path must not be a symlink: {path}")
            self.assert_mode(path, 0o700 if path.is_dir() else 0o600)

    def assert_safe_lower_scope_failure(self, failure: dict) -> None:
        serialized = json.dumps(failure, sort_keys=True).casefold()
        self.assertNotIn(CANARY.casefold(), serialized)
        for managed_root in (self.root, self.runtime, self.media, self.catalogue):
            self.assertNotIn(str(managed_root).casefold(), serialized)
            self.assertNotIn(str(managed_root.resolve()).casefold(), serialized)

    def record_two_public_items(self) -> tuple[str, str]:
        item_ids: list[str] = []
        for number in ("one", "two"):
            arguments = [
                "order",
                "--actor",
                "Privacy integrity test",
                "--source-ref",
                "Public fixture provenance",
                "--name",
                f"Public compatibility fixture {number}",
                "--category",
                "test fixture",
                "--ordered-on",
                "2026-08-06",
                "--order-placed",
                "--sensitivity",
                "low",
            ]
            if number == "two":
                arguments.insert(0, "--continue-batch")
            recorded = self.execute(*arguments)
            item_ids.append(recorded["result"]["item_id"])
        return item_ids[0], item_ids[1]

    def corrupt_canonical_store(self) -> None:
        (self.root / "Data" / "store" / "items.jsonl").write_text(
            '{"item_id":"' + CANARY + '"\n', encoding="utf-8"
        )

    def test_fresh_init_makes_every_managed_product_path_owner_private(self) -> None:
        self.initialize()

        self.assert_private_tree(self.root)
        self.assert_private_tree(self.runtime)
        self.assert_mode(self.catalogue, 0o600)
        self.assert_mode(self.runtime / "inventory.sqlite", 0o600)
        for table in (self.root / "Data" / "store").glob("*.jsonl"):
            self.assert_mode(table, 0o600)

    def test_mutation_backups_and_exports_are_owner_private(self) -> None:
        self.initialize()
        mutated = self.execute(
            "add-location",
            "--location-id",
            "loc-private-mode",
            "--name",
            "Private mode fixture",
            "--kind",
            "room",
        )
        backup = Path(mutated["backup"])
        export = self.scratch / "inventory-export.tar.gz"
        insurance = self.scratch / "insurance-export.zip"

        self.execute("export", "--output", str(export))
        self.execute("insurance-export", "--output", str(insurance))

        self.assert_private_tree(self.root)
        self.assert_private_tree(self.runtime)
        self.assert_private_tree(backup)
        self.assert_mode(export, 0o600)
        self.assert_mode(insurance, 0o600)

    def test_insurance_export_refuses_a_generation_after_status_detects_store_tampering(self) -> None:
        self.initialize()
        self.corrupt_canonical_store()

        status = self.execute("status", succeeds=False)
        self.assertIn("item", json.dumps(status).casefold())
        output = self.scratch / "must-not-exist-insurance.zip"
        refused = self.execute("insurance-export", "--output", str(output), succeeds=False)

        self.assertFalse(output.exists())
        self.assertIn("item", json.dumps(refused).casefold())

    def test_authoritative_reads_fail_closed_and_lower_scopes_never_leak_tampered_data(self) -> None:
        self.initialize()
        first_item, second_item = self.record_two_public_items()
        self.corrupt_canonical_store()

        lower_scope_commands = (
            ("compatibility", first_item, second_item),
            ("maintenance-report",),
            ("insurance-status",),
        )
        for scope in ("public", "personal"):
            for command in lower_scope_commands:
                with self.subTest(scope=scope, command=command[0]):
                    failure = self.execute(*command, scope=scope, succeeds=False)
                    self.assert_safe_lower_scope_failure(failure)

        for command in (
            ("export", "--output", str(self.scratch / "must-not-exist-export.tar.gz")),
            ("insurance-export", "--output", str(self.scratch / "must-not-exist-insurance.zip")),
            ("capture-status", "capture-must-not-be-served"),
            (
                "capture-prepare",
                "--overview",
                str(HERE / "tests" / "fixtures" / "capture" / "synthetic-benchmark.json"),
                "--captured-on",
                "2026-08-06",
                "--segments",
                "[]",
                "--source-ref",
                "Tampered-store capture fixture",
            ),
        ):
            with self.subTest(command=command[0]):
                self.execute(*command, succeeds=False)


if __name__ == "__main__":
    unittest.main()
