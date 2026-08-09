"""Focused Batch 11 upkeep-measurement tests."""

from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from property_inventory.cli import prepare_maintenance_marker_parent
from property_inventory.maintenance import (
    MaintenanceError,
    maintenance_report,
    measured_elapsed_seconds,
    run_synthetic_four_week_harness,
)


def fixture_rows() -> dict[str, list[dict[str, object]]]:
    sessions = []
    links = []
    for index, performed_on in enumerate(
        ("2026-07-06", "2026-07-13", "2026-07-20", "2026-07-27"), start=1
    ):
        session_id = f"maintenance-{index}"
        sessions.append(
            {
                "maintenance_session_id": session_id,
                "performed_on": performed_on,
                "activity": "synthetic upkeep fixture",
                "elapsed_seconds": index * 10,
                "correction_count": index - 1,
                "review_count": index,
                "evidence_id": "ev-visible",
                "sensitivity": "low",
                "notes": None,
            }
        )
        links.extend(
            (
                {"maintenance_session_id": session_id, "item_id": "item-visible"},
                {"maintenance_session_id": session_id, "item_id": "item-visible-2"},
            )
        )
    sessions.append(
        {
            "maintenance_session_id": "maintenance-hidden",
            "performed_on": "2026-07-27",
            "activity": "PRIVATE-MAINTENANCE-CANARY",
            "elapsed_seconds": 999,
            "correction_count": 999,
            "review_count": 999,
            "evidence_id": "ev-hidden",
            "sensitivity": "high",
            "notes": None,
        }
    )
    return {
        "items": [
            {"item_id": "item-visible", "sensitivity": "low"},
            {"item_id": "item-visible-2", "sensitivity": "low"},
            {"item_id": "item-hidden", "sensitivity": "high"},
        ],
        "evidence": [
            {"evidence_id": "ev-visible", "sensitivity": "low"},
            {"evidence_id": "ev-hidden", "sensitivity": "high"},
        ],
        "maintenance_sessions": sessions,
        "maintenance_session_items": links,
    }


class MaintenanceTests(unittest.TestCase):
    def test_first_marker_directory_is_fsynced_from_its_runtime_parent(self) -> None:
        with tempfile.TemporaryDirectory(prefix="inventory-maintenance-parent-") as temporary:
            runtime = Path(temporary)
            marker = runtime / "maintenance-sessions" / "maintenance-test.json"
            with patch("property_inventory.cli.fsync_directory") as fsync:
                prepare_maintenance_marker_parent(runtime, marker)
            fsync.assert_called_once_with(runtime)
            self.assertTrue(marker.parent.is_dir())

    def test_elapsed_uses_one_monotonic_duration_and_detects_clock_discontinuity(self) -> None:
        self.assertEqual(
            measured_elapsed_seconds(
                started_at="2026-08-06T01:00:00+01:00",
                started_monotonic_ns=10_000_000_000,
                finished_at="2026-08-06T01:00:12+01:00",
                finished_monotonic_ns=22_000_000_000,
            ),
            12,
        )
        with self.assertRaisesRegex(MaintenanceError, "clocks disagree"):
            measured_elapsed_seconds(
                started_at="2026-08-06T01:00:00+01:00",
                started_monotonic_ns=10,
                finished_at="2026-08-06T02:00:00+01:00",
                finished_monotonic_ns=20,
            )
        self.assertEqual(
            measured_elapsed_seconds(
                started_at="invalid",
                started_monotonic_ns=-1,
                finished_at="invalid",
                finished_monotonic_ns=-1,
                explicit_elapsed_seconds=7,
            ),
            7,
        )

    def test_scope_filtering_happens_before_counts_and_elapsed_is_not_per_item(self) -> None:
        report = maintenance_report(fixture_rows(), scope="public")
        self.assertEqual(
            report["summary"],
            {
                "session_count": 4,
                "elapsed_seconds": 100,
                "correction_count": 6,
                "review_count": 10,
                "item_link_count": 8,
                "observed_week_count": 4,
            },
        )
        self.assertEqual(sum(week["elapsed_seconds"] for week in report["weeks"]), 100)
        self.assertNotIn("PRIVATE-MAINTENANCE-CANARY", str(report))

    def test_zero_item_system_session_and_empty_scope_are_valid(self) -> None:
        rows = fixture_rows()
        rows["maintenance_session_items"] = []
        report = maintenance_report(rows, scope="public")
        self.assertEqual(report["summary"]["item_link_count"], 0)
        rows["maintenance_sessions"] = [rows["maintenance_sessions"][-1]]
        hidden = maintenance_report(rows, scope="public")
        self.assertEqual(hidden["summary"]["session_count"], 0)
        self.assertIn("not proof", hidden["meaning_if_empty"])

    def test_malformed_scope_and_sensitivity_fail_as_controlled_errors(self) -> None:
        with self.assertRaisesRegex(MaintenanceError, "scope must be a string"):
            maintenance_report(fixture_rows(), scope=[])  # type: ignore[arg-type]
        rows = fixture_rows()
        rows["items"][0]["sensitivity"] = []
        with self.assertRaisesRegex(MaintenanceError, "invalid sensitivity"):
            maintenance_report(rows, scope="private")

    def test_checked_four_week_fixture_is_explicitly_synthetic_only(self) -> None:
        fixture_path = (
            Path(__file__).resolve().parents[1]
            / "tests" / "fixtures"
            / "maintenance"
            / "synthetic-four-weeks.json"
        )
        fixture = json.loads(fixture_path.read_text(encoding="utf-8"))
        result = run_synthetic_four_week_harness(fixture)
        self.assertEqual(result["claim"], "synthetic-fixture-only")
        self.assertEqual(result["week_count"], 4)
        fixture["corpus_label"] = "real-user"
        with self.assertRaisesRegex(MaintenanceError, "explicitly synthetic"):
            run_synthetic_four_week_harness(fixture)


if __name__ == "__main__":
    unittest.main()
