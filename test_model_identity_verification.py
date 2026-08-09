"""Regression coverage for conservative model-identity verification."""

from __future__ import annotations

import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

HERE = Path(__file__).resolve().parent
CLI = HERE / "property_inventory.py"


class ModelIdentityVerificationTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory(prefix="inventory-model-identity-")
        self.scratch = Path(self.temp.name)
        self.root = self.scratch / "inventory"
        self.runtime = self.scratch / "runtime"
        self.cli("init")
        self.cli(
            "add-location", "--location-id", "loc-home", "--name", "Home",
            "--kind", "place", "--sensitivity", "low",
        )

    def tearDown(self) -> None:
        self.temp.cleanup()

    def command(self, *arguments: str) -> list[str]:
        return [
            sys.executable,
            str(CLI),
            "--inventory-root",
            str(self.root),
            "--runtime-dir",
            str(self.runtime),
            "--scope",
            "private",
            *arguments,
        ]

    def cli(self, *arguments: str) -> dict:
        complete = subprocess.run(
            self.command(*arguments), text=True, capture_output=True, check=False
        )
        self.assertEqual(complete.returncode, 0, complete.stderr or complete.stdout)
        return json.loads(complete.stdout)

    def cli_fails(self, *arguments: str) -> dict:
        complete = subprocess.run(
            self.command(*arguments), text=True, capture_output=True, check=False
        )
        self.assertNotEqual(complete.returncode, 0, complete.stdout)
        return json.loads(complete.stderr)

    def discover_model(self, *, brand: str, model: str, source_ref: str) -> dict:
        return self.cli(
            "discover", "--actor", "model identity test", "--source-ref", source_ref,
            "--name", "Generic USB charger", "--category", "charger",
            "--checked-on", "2026-08-06", "--location-id", "loc-home",
            "--brand", brand, "--model", model, "--specs", '{"watts":20}',
            "--new-model", "--sensitivity", "low",
        )

    def test_distinct_brand_and_model_share_generic_label_and_specs(self) -> None:
        self.discover_model(brand="Arc", model="Charge 20", source_ref="Arc charger")
        self.discover_model(brand="Beacon", model="Charge 20", source_ref="Beacon charger")

        rows = [
            json.loads(line)
            for line in (self.root / "Data" / "store" / "models.jsonl").read_text().splitlines()
        ]
        self.assertEqual(
            {(row["brand"], row["model"]) for row in rows},
            {("Arc", "Charge 20"), ("Beacon", "Charge 20")},
        )
        self.assertEqual(self.cli("status")["verification"]["failures"], [])

    def test_exact_product_identity_duplicate_is_rejected(self) -> None:
        self.discover_model(brand="Arc", model="Charge 20", source_ref="first charger")

        failed = self.cli_fails(
            "discover", "--actor", "model identity test", "--source-ref", "second charger",
            "--name", "Generic USB charger", "--category", "charger",
            "--checked-on", "2026-08-06", "--location-id", "loc-home",
            "--brand", "Arc", "--model", "Charge 20", "--specs", '{"watts":20}',
            "--new-model", "--sensitivity", "low",
        )

        self.assertIn("exact product-identity duplicate models remain", failed["error"])

    def test_case_only_identity_difference_is_still_a_duplicate(self) -> None:
        self.discover_model(brand="Arc", model="Charge 20", source_ref="first charger")

        failed = self.cli_fails(
            "discover", "--actor", "model identity test",
            "--source-ref", "case-only duplicate charger",
            "--name", "GENERIC USB CHARGER", "--category", "CHARGER",
            "--checked-on", "2026-08-06", "--location-id", "loc-home",
            "--brand", "ARC", "--model", "CHARGE 20",
            "--specs", '{"watts":20}', "--new-model", "--sensitivity", "low",
        )

        self.assertIn("exact product-identity duplicate models remain", failed["error"])


if __name__ == "__main__":
    unittest.main()
