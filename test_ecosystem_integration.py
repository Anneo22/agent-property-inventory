#!/usr/bin/env python3
"""End-to-end CLI coverage for ecosystem import, policy, and recovery drill."""

from __future__ import annotations

import hashlib
import json
import stat
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from property_inventory import cli as inventory_cli

HERE = Path(__file__).resolve().parent
CLI = HERE / "property_inventory.py"
FIXTURES = HERE / "test_fixtures" / "imports"


class EcosystemCliTest(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory(prefix="inventory-ecosystem-cli-")
        self.scratch = Path(self.temporary.name)
        self.root = self.scratch / "inventory"
        self.runtime = self.scratch / "runtime"
        self.media = self.scratch / "media"
        self.catalogue = self.scratch / "catalogue" / "Inventory.md"
        self.cli("init")

    def tearDown(self) -> None:
        self.temporary.cleanup()

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

    def cli(self, *arguments: str, scope: str = "private") -> dict:
        completed = subprocess.run(
            self.command(*arguments, scope=scope), text=True, capture_output=True, check=False
        )
        self.assertEqual(completed.returncode, 0, completed.stderr or completed.stdout)
        return json.loads(completed.stdout)

    def fails(self, *arguments: str, scope: str = "private") -> dict:
        completed = subprocess.run(
            self.command(*arguments, scope=scope), text=True, capture_output=True, check=False
        )
        self.assertNotEqual(completed.returncode, 0, completed.stdout)
        return json.loads(completed.stderr)

    def canonical_digest(self) -> str:
        digest = hashlib.sha256()
        for source in sorted((self.root / "Data" / "store").glob("*.jsonl")):
            digest.update(source.name.encode())
            digest.update(source.read_bytes())
        return digest.hexdigest()

    def import_propose(self, source: Path, namespace: str = "shop-a") -> dict:
        return self.cli(
            "import-propose",
            "--input",
            str(source),
            "--format",
            source.suffix.removeprefix("."),
            "--source-name",
            source.name,
            "--source-namespace",
            namespace,
            "--source-date",
            "2026-08-06",
        )

    def test_import_propose_is_normal_review_only_proposal(self) -> None:
        before = self.canonical_digest()
        prepared = self.import_propose(FIXTURES / "cart-and-order.csv")
        self.assertEqual(prepared["status"], "prepared")
        self.assertEqual(self.canonical_digest(), before)
        proposal = prepared["proposal"]
        self.assertEqual(proposal["import"]["source_namespace"], "shop-a")
        operations = proposal["operations"]
        self.assertEqual([operation[0] for operation in operations], ["plan", "order"])
        for operation in operations:
            self.assertNotIn("receive", operation)
            self.assertNotIn("physical-check", operation)
            self.assertNotIn("--location-id", operation)
            self.assertNotIn("--condition", operation)

    def test_import_propose_rejects_known_namespace_collision_and_bad_input(self) -> None:
        provenance = {"import": {"source_namespace": "shop-a", "external_id": "order-1"}}
        self.cli(
            "order",
            "--actor",
            "ecosystem test",
            "--source-ref",
            "fixture order",
            "--name",
            "existing imported item",
            "--category",
            "test",
            "--ordered-on",
            "2026-08-06",
            "--order-placed",
            "--specs",
            json.dumps(provenance),
        )
        error = self.fails(
            "import-propose",
            "--input",
            str(FIXTURES / "cart-and-order.csv"),
            "--format",
            "csv",
            "--source-name",
            "cart-and-order.csv",
            "--source-namespace",
            "shop-a",
            "--source-date",
            "2026-08-06",
        )
        self.assertIn("collides with an existing", error["error"])
        accepted_elsewhere = self.import_propose(FIXTURES / "cart-and-order.csv", "shop-b")
        self.assertEqual(accepted_elsewhere["status"], "prepared")
        malformed = self.scratch / "malformed.csv"
        malformed.write_bytes(b"status,name,date\ncart,A\x00B,2026-08-06\n")
        error = self.fails(
            "import-propose",
            "--input",
            str(malformed),
            "--format",
            "csv",
            "--source-name",
            malformed.name,
            "--source-namespace",
            "shop-c",
            "--source-date",
            "2026-08-06",
        )
        self.assertIn("NUL", error["error"])

    def test_applied_import_identity_survives_existing_model_reuse(self) -> None:
        ordered = self.cli(
            "order", "--actor", "ecosystem test", "--source-ref", "fixture order",
            "--name", "Chain tool", "--category", "test", "--ordered-on", "2026-08-06",
            "--order-placed",
        )["result"]
        self.cli(
            "add-location", "--location-id", "loc-ecosystem", "--name", "Ecosystem fixture",
            "--kind", "room", "--sensitivity", "personal",
        )
        self.cli(
            "receive", "--actor", "ecosystem test", "--source-ref", "physical fixture",
            "--item-id", ordered["item_id"], "--received-on", "2026-08-06", "--physical-check",
            "--location-id", "loc-ecosystem",
        )
        source = self.scratch / "existing-model.json"
        source.write_text(
            json.dumps(
                [
                    {
                        "status": "ordered",
                        "name": "Chain tool",
                        "category": "test",
                        "date": "2026-08-06",
                        "external_id": "external-77",
                    }
                ]
            ),
            encoding="utf-8",
        )
        prepared = self.import_propose(source)
        self.cli("proposal-apply", prepared["proposal"]["proposal_id"])
        rejected = self.fails(
            "import-propose", "--input", str(source), "--format", "json",
            "--source-name", source.name, "--source-namespace", "shop-a",
            "--source-date", "2026-08-06",
        )
        self.assertIn("collides with an existing", rejected["error"])
        model = next(
            json.loads(line)
            for line in (self.root / "Data" / "store" / "models.jsonl").read_text().splitlines()
            if json.loads(line)["name"] == "Chain tool"
        )
        self.assertEqual(json.loads(model["specs_json"]), {})

    def test_doctor_retains_archive_and_compatibility_matrix_is_executable(self) -> None:
        catalogue_before = self.catalogue.read_bytes()
        archive = self.scratch / "portable" / "doctor.tar.gz"
        report = self.cli("doctor", "--output", str(archive))
        self.assertEqual(report["status"], "pass")
        self.assertTrue(archive.is_file())
        self.assertEqual(stat.S_IMODE(archive.stat().st_mode), 0o600)
        self.assertTrue(report["restored_roots_cleaned"])
        self.assertEqual(self.catalogue.read_bytes(), catalogue_before)
        self.assertEqual(
            [row["label"] for row in report["commands"]], ["export", "restore", "status"]
        )
        matrix = self.cli("compatibility-status")
        self.assertEqual(matrix["status"], "pass")
        self.assertEqual(
            [row["schema_version"] for row in matrix["entries"]], [1, 2, 3, 4, 5, 6]
        )
        error = self.fails("doctor", "--output", str(self.root / "unsafe.tar.gz"))
        self.assertIn("outside managed root", error["error"])

    def test_export_rejects_staging_substitution_without_touching_its_target(self) -> None:
        output = self.scratch / "hostile-export.tar.gz"
        victim = self.scratch / "victim.txt"
        victim.write_text("must remain unchanged", encoding="utf-8")
        original_link = inventory_cli.os.link

        def substitute_then_link(
            source: Path, destination: Path, *, follow_symlinks: bool
        ) -> None:
            source.unlink()
            source.symlink_to(victim)
            original_link(
                source,
                destination,
                follow_symlinks=follow_symlinks,
            )

        arguments = self.command("export", "--output", str(output))[2:]
        with patch.object(
            inventory_cli.os,
            "link",
            side_effect=substitute_then_link,
        ):
            with self.assertRaisesRegex(
                inventory_cli.InventoryError,
                "publication changed before verification",
            ):
                inventory_cli.execute(arguments)
        self.assertEqual(victim.read_text(encoding="utf-8"), "must remain unchanged")
        self.assertTrue(output.is_symlink())


if __name__ == "__main__":
    unittest.main()
