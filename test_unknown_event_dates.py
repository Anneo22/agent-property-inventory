"""Lifecycle dates preserve unknown occurrence separately from observation."""

from __future__ import annotations

import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

HERE = Path(__file__).resolve().parent
CLI = HERE / "property_inventory.py"


class UnknownEventDateTest(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory(prefix="inventory-unknown-date-")
        scratch = Path(self.temp.name)
        self.root = scratch / "inventory"
        self.runtime = scratch / "runtime"
        self.media = scratch / "media"
        self.catalogue = scratch / "Inventory.md"
        self.cli("init")
        self.cli(
            "add-location",
            "--location-id",
            "loc-test",
            "--name",
            "Test room",
            "--kind",
            "room",
            "--sensitivity",
            "low",
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
            "--media-root",
            str(self.media),
            "--catalogue-output",
            str(self.catalogue),
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

    def failed(self, *arguments: str) -> dict:
        complete = subprocess.run(
            self.command(*arguments), text=True, capture_output=True, check=False
        )
        self.assertNotEqual(complete.returncode, 0, complete.stdout)
        return json.loads(complete.stderr)

    def discover(self, name: str) -> str:
        return self.cli(
            "discover",
            "--actor",
            "unknown date test",
            "--source-ref",
            f"physical check of {name}",
            "--name",
            name,
            "--category",
            "test fixture",
            "--checked-on",
            "2026-08-05",
            "--location-id",
            "loc-test",
            "--new-unit",
            "--condition",
            "working",
            "--sensitivity",
            "low",
        )["result"]["item_id"]

    def test_unknown_sale_date_is_null_and_observation_date_is_explicit(self) -> None:
        item_id = self.discover("Unknown-date sold item")
        result = self.cli(
            "sell",
            "--actor",
            "unknown date test",
            "--source-ref",
            "explicit statement that the item was sold, date unknown",
            "--item-id",
            item_id,
            "--sold-date-unknown",
            "--observed-on",
            "2026-08-06",
        )["result"]
        self.assertIsNone(result["occurred_on"])
        self.assertEqual(result["observed_on"], "2026-08-06")
        self.assertEqual(result["occurred_on_precision"], "unknown")
        event = self.cli("show", item_id)["events"][-1]
        self.assertIsNone(event["occurred_on"])
        self.assertEqual(event["observed_on"], "2026-08-06")
        self.assertEqual(event["occurred_on_precision"], "unknown")
        self.assertEqual(self.cli("status")["status"], "pass")

    def test_generic_unknown_date_and_argument_pairing_are_enforced(self) -> None:
        item_id = self.discover("Unknown-date gifted item")
        missing_observation = self.failed(
            "change",
            "--actor",
            "unknown date test",
            "--source-ref",
            "gift statement",
            "--item-id",
            item_id,
            "--event-type",
            "gifted",
            "--date-unknown",
        )
        self.assertIn("requires --observed-on", missing_observation["error"])
        result = self.cli(
            "change",
            "--actor",
            "unknown date test",
            "--source-ref",
            "gift statement",
            "--item-id",
            item_id,
            "--event-type",
            "gifted",
            "--date-unknown",
            "--observed-on",
            "2026-08-07",
        )["result"]
        self.assertEqual(result["ownership_state"], "disposed")
        self.assertIsNone(result["occurred_on"])
        self.assertEqual(result["observed_on"], "2026-08-07")
        self.assertEqual(self.cli("status")["status"], "pass")

    def test_unknown_date_does_not_mask_the_latest_exact_lifecycle_date(self) -> None:
        item_id = self.discover("Unknown-date chronology item")
        self.cli(
            "sell",
            "--actor",
            "unknown date test",
            "--source-ref",
            "sale confirmed but its historical date is unknown",
            "--item-id",
            item_id,
            "--sold-date-unknown",
            "--observed-on",
            "2026-08-06",
        )
        events_before = self.cli("show", item_id)["events"]
        rejected = self.failed(
            "restore-current-ownership",
            "--actor",
            "unknown date test",
            "--source-ref",
            "physical check with an impossible earlier restore date",
            "--item-id",
            item_id,
            "--checked-on",
            "2020-01-01",
            "--location-id",
            "loc-test",
            "--reason",
            "reacquired",
        )
        self.assertIn("latest lifecycle event with an exact date", rejected["error"])
        shown = self.cli("show", item_id)
        self.assertEqual(shown["item"]["ownership_state"], "disposed")
        self.assertEqual(shown["events"], events_before)
        self.assertEqual(self.cli("status")["status"], "pass")

    def test_stale_unknown_date_fact_cannot_override_later_exact_reality(self) -> None:
        item_id = self.discover("Stale unknown-date sale item")
        events_before = self.cli("show", item_id)["events"]
        rejected = self.failed(
            "sell",
            "--actor",
            "unknown date test",
            "--source-ref",
            "old note with no known sale date",
            "--item-id",
            item_id,
            "--sold-date-unknown",
            "--observed-on",
            "2020-01-01",
        )
        self.assertIn("predates the latest lifecycle event with an exact date", rejected["error"])
        shown = self.cli("show", item_id)
        self.assertEqual(shown["item"]["ownership_state"], "confirmed")
        self.assertEqual(shown["events"], events_before)
        self.assertEqual(self.cli("status")["status"], "pass")


if __name__ == "__main__":
    unittest.main()
