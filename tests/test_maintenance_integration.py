#!/usr/bin/env python3
"""End-to-end maintenance timer, transaction, and synthetic-harness tests."""

from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

from property_inventory.cli import maintenance_marker_digest

HERE = Path(__file__).resolve().parents[1]
CLI = HERE / "property_inventory.py"
FIXTURE = HERE / "tests" / "fixtures" / "maintenance" / "synthetic-four-weeks.json"


class MaintenanceIntegrationTest(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory(prefix="inventory-maintenance-")
        self.scratch = Path(self.temporary.name)
        self.root = self.scratch / "inventory"
        self.runtime = self.scratch / "runtime"
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
            "--scope",
            scope,
            *arguments,
        ]

    def cli(self, *arguments: str, scope: str = "private", env: dict[str, str] | None = None) -> dict:
        completed = subprocess.run(
            self.command(*arguments, scope=scope),
            text=True,
            capture_output=True,
            check=False,
            env={**os.environ, **(env or {})},
        )
        self.assertEqual(completed.returncode, 0, completed.stderr or completed.stdout)
        return json.loads(completed.stdout)

    def fails(self, *arguments: str, scope: str = "private", env: dict[str, str] | None = None) -> dict:
        completed = subprocess.run(
            self.command(*arguments, scope=scope),
            text=True,
            capture_output=True,
            check=False,
            env={**os.environ, **(env or {})},
        )
        self.assertNotEqual(completed.returncode, 0, completed.stdout)
        return json.loads(completed.stderr)

    def item(self, name: str, sensitivity: str = "personal") -> tuple[str, str]:
        result = self.cli(
            "order",
            "--actor",
            "maintenance fixture",
            "--source-ref",
            f"order for {name}",
            "--name",
            name,
            "--category",
            "fixture",
            "--ordered-on",
            "2026-08-06",
            "--order-placed",
            "--sensitivity",
            sensitivity,
        )["result"]
        return result["item_id"], result["evidence_id"]

    def start(self, *, evidence_id: str | None = None, sensitivity: str = "personal") -> dict:
        arguments = [
            "maintenance-start",
            "--performed-on",
            "2026-08-06",
            "--activity",
            "fixture upkeep",
            "--sensitivity",
            sensitivity,
        ]
        arguments.extend(("--evidence-id", evidence_id) if evidence_id else ("--source-ref", "checked upkeep source"))
        return self.cli(*arguments)

    def finish(self, session_id: str, *extra: str, env: dict[str, str] | None = None) -> dict:
        return self.cli(
            "maintenance-finish", session_id, "--elapsed-seconds", "9",
            "--correction-count", "0", "--review-count", "0", *extra, env=env,
        )

    def test_zero_and_multi_item_sessions_create_no_lifecycle_events(self) -> None:
        first_item, _ = self.item("first upkeep item")
        second_item, _ = self.item("second upkeep item")
        events_before = (self.root / "Data" / "store" / "inventory_events.jsonl").read_bytes()
        zero = self.start()
        self.finish(zero["maintenance_session_id"])
        multi = self.start()
        completed = self.finish(
            multi["maintenance_session_id"], "--item-id", first_item, "--item-id", second_item
        )
        self.assertFalse(completed["result"]["recovered"])
        self.assertEqual((self.root / "Data" / "store" / "inventory_events.jsonl").read_bytes(), events_before)
        links = (self.root / "Data" / "store" / "maintenance_session_items.jsonl").read_text()
        self.assertIn(first_item, links)
        self.assertIn(second_item, links)
        evidence_links = (self.root / "Data" / "store" / "item_evidence.jsonl").read_text()
        self.assertIn(completed["result"]["evidence_id"], evidence_links)

    def test_marker_tamper_clock_discontinuity_and_committed_retry(self) -> None:
        started = self.start()
        marker = Path(started["marker"])
        marker.write_text("{}\n")
        self.assertIn(
            "marker is malformed",
            self.fails(
                "maintenance-finish", started["maintenance_session_id"],
                "--correction-count", "0", "--review-count", "0",
            )["error"],
        )
        marker.unlink()
        marker.symlink_to(self.scratch / "missing-marker")
        self.assertIn(
            "traverses a symlink",
            self.fails(
                "maintenance-finish", started["maintenance_session_id"],
                "--correction-count", "0", "--review-count", "0",
            )["error"],
        )
        marker.unlink()
        restarted = self.start()
        marker = Path(restarted["marker"])
        payload = json.loads(marker.read_text())
        payload["activity"] = "tampered but valid activity"
        marker.write_text(json.dumps(payload) + "\n")
        self.assertIn(
            "marker is malformed",
            self.fails(
                "maintenance-finish", restarted["maintenance_session_id"],
                "--correction-count", "0", "--review-count", "0",
            )["error"],
        )
        payload = json.loads(marker.read_text())
        payload["activity"] = "fixture upkeep"
        payload["started_monotonic_ns"] = 10**30
        payload["record_digest"] = maintenance_marker_digest(payload)
        marker.write_text(json.dumps(payload) + "\n")
        self.assertIn(
            "explicit_elapsed_seconds",
            self.fails(
                "maintenance-finish", restarted["maintenance_session_id"],
                "--correction-count", "0", "--review-count", "0",
            )["error"],
        )
        self.finish(restarted["maintenance_session_id"])
        crash = self.start()
        self.fails(
            "maintenance-finish",
            crash["maintenance_session_id"],
            "--elapsed-seconds",
            "8",
            "--correction-count",
            "1",
            "--review-count",
            "2",
            env={"PROPERTY_INVENTORY_RAISE_AFTER_COMMIT": "1"},
        )
        mismatch = self.fails(
            "maintenance-finish", crash["maintenance_session_id"], "--elapsed-seconds", "999",
            "--correction-count", "999", "--review-count", "999",
            "--item-id", "missing-item",
        )
        self.assertIn("disagrees with the durable finish request", mismatch["error"])
        recovered = self.cli(
            "maintenance-finish", crash["maintenance_session_id"], "--elapsed-seconds", "8",
            "--correction-count", "1", "--review-count", "2",
        )
        self.assertTrue(recovered["result"]["recovered"])
        completed_marker = json.loads(Path(crash["marker"]).read_text())
        self.assertEqual(completed_marker["status"], "completed")
        self.assertEqual(completed_marker["result"]["correction_count"], 1)

    def test_completed_marker_survives_lost_response_and_unknown_counts_are_rejected(self) -> None:
        missing_counts = self.start()
        error = self.fails(
            "maintenance-finish", missing_counts["maintenance_session_id"],
            "--elapsed-seconds", "3",
        )
        self.assertIn("unknown is not zero", error["error"])

        started = self.start()
        crashed = subprocess.run(
            self.command(
                "maintenance-finish", started["maintenance_session_id"],
                "--elapsed-seconds", "4", "--correction-count", "0", "--review-count", "1",
            ),
            text=True,
            capture_output=True,
            check=False,
            env={**os.environ, "PROPERTY_INVENTORY_FAIL_MAINTENANCE_AFTER_MARKER": "1"},
        )
        self.assertEqual(crashed.returncode, 96)
        recovered = self.cli(
            "maintenance-finish", started["maintenance_session_id"],
            "--elapsed-seconds", "4", "--correction-count", "0", "--review-count", "1",
        )
        self.assertTrue(recovered["result"]["recovered"])
        sessions = (self.root / "Data" / "store" / "maintenance_sessions.jsonl").read_text().splitlines()
        self.assertEqual(
            sum(started["maintenance_session_id"] in line for line in sessions), 1
        )

    def test_invalid_first_finish_request_does_not_poison_the_timer(self) -> None:
        item_id, item_evidence_id = self.item("valid upkeep item")
        invalid_item = self.start()
        error = self.fails(
            "maintenance-finish",
            invalid_item["maintenance_session_id"],
            "--elapsed-seconds",
            "4",
            "--correction-count",
            "0",
            "--review-count",
            "0",
            "--item-id",
            "itm-missing",
        )
        self.assertIn("expected one items row", error["error"])
        recovered = self.finish(invalid_item["maintenance_session_id"], "--item-id", item_id)
        self.assertFalse(recovered["result"]["recovered"])

        unsupported = self.start(evidence_id=item_evidence_id)
        other_item, _ = self.item("unsupported upkeep item")
        error = self.fails(
            "maintenance-finish",
            unsupported["maintenance_session_id"],
            "--elapsed-seconds",
            "4",
            "--correction-count",
            "0",
            "--review-count",
            "0",
            "--item-id",
            other_item,
        )
        self.assertIn("must already support", error["error"])
        recovered = self.finish(
            unsupported["maintenance_session_id"], "--item-id", item_id
        )
        self.assertFalse(recovered["result"]["recovered"])

    def test_scope_report_and_synthetic_harness(self) -> None:
        visible = self.start(sensitivity="low")
        self.finish(visible["maintenance_session_id"])
        hidden = self.start(sensitivity="high")
        self.finish(hidden["maintenance_session_id"])
        public = self.cli("maintenance-report", scope="public")
        self.assertEqual(public["summary"]["session_count"], 1)
        self.assertNotIn(hidden["maintenance_session_id"], str(public))
        harness = self.cli("maintenance-harness", "--input", str(FIXTURE))
        self.assertEqual(harness["claim"], "synthetic-fixture-only")
        self.assertEqual(harness["week_count"], 4)


if __name__ == "__main__":
    unittest.main()
