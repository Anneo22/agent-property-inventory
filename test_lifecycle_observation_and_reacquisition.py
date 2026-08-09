"""Regression coverage for lifecycle observation dates and new ownership episodes."""

from __future__ import annotations

import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

HERE = Path(__file__).resolve().parent
CLI = HERE / "property_inventory.py"


class LifecycleObservationAndReacquisitionTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory(prefix="inventory-lifecycle-semantics-")
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
        completed = subprocess.run(
            self.command(*arguments), text=True, capture_output=True, check=False
        )
        self.assertEqual(completed.returncode, 0, completed.stderr or completed.stdout)
        return json.loads(completed.stdout)

    def failed(self, *arguments: str) -> dict:
        completed = subprocess.run(
            self.command(*arguments), text=True, capture_output=True, check=False
        )
        self.assertNotEqual(completed.returncode, 0, completed.stdout)
        return json.loads(completed.stderr)

    def ordered_and_received(self, name: str, *, quantity: str | None = None) -> str:
        order = [
            "order",
            "--actor",
            "Lifecycle semantics test",
            "--source-ref",
            f"2020 order for {name}",
            "--name",
            name,
            "--category",
            "test fixture",
            "--ordered-on",
            "2020-01-01",
            "--order-placed",
            "--new-model",
            "--location-id",
            "loc-test",
            "--purchase-price",
            "42",
            "--purchase-currency",
            "GBP",
            "--receipt-ref",
            f"old receipt for {name}",
            "--sensitivity",
            "low",
        ]
        if quantity is not None:
            order.extend(("--quantity", quantity))
        item_id = self.cli(*order)["result"]["item_id"]
        self.cli(
            "receive",
            "--actor",
            "Lifecycle semantics test",
            "--source-ref",
            f"2020 physical receipt of {name}",
            "--item-id",
            item_id,
            "--received-on",
            "2020-01-02",
            "--location-id",
            "loc-test",
            "--condition",
            "working",
            "--serial-or-lot",
            f"serial-{name}",
            "--physical-check",
        )
        return item_id

    def sell(self, item_id: str, *, observed_on: str = "2026-08-06") -> dict:
        return self.cli(
            "sell",
            "--actor",
            "Lifecycle semantics test",
            "--source-ref",
            "historical sale record reviewed later",
            "--item-id",
            item_id,
            "--sold-on",
            "2025-01-02",
            "--observed-on",
            observed_on,
        )["result"]

    def test_exact_event_preserves_a_later_observation_date(self) -> None:
        item_id = self.ordered_and_received("Observed sale")
        result = self.sell(item_id)
        self.assertEqual(result["occurred_on"], "2025-01-02")
        self.assertEqual(result["observed_on"], "2026-08-06")
        self.assertEqual(result["occurred_on_precision"], "exact")
        shown = self.cli("show", item_id)
        event = shown["events"][-1]
        self.assertEqual(event["occurred_on"], "2025-01-02")
        self.assertEqual(event["observed_on"], "2026-08-06")
        event_evidence = next(
            row for row in shown["evidence"] if row["evidence_id"] == event["evidence_id"]
        )
        self.assertEqual(event_evidence["captured_on"], "2026-08-06")
        self.assertEqual(self.cli("status")["status"], "pass")

    def test_generic_exact_lifecycle_event_preserves_later_observation(self) -> None:
        item_id = self.ordered_and_received("Observed gift")
        result = self.cli(
            "change",
            "--actor",
            "Lifecycle semantics test",
            "--source-ref",
            "historical gift statement reviewed later",
            "--item-id",
            item_id,
            "--event-type",
            "gifted",
            "--occurred-on",
            "2025-01-02",
            "--observed-on",
            "2026-08-06",
        )["result"]
        self.assertEqual(result["occurred_on"], "2025-01-02")
        self.assertEqual(result["observed_on"], "2026-08-06")
        event = self.cli("show", item_id)["events"][-1]
        self.assertEqual(event["occurred_on"], "2025-01-02")
        self.assertEqual(event["observed_on"], "2026-08-06")

    def test_observation_before_occurrence_is_rejected_without_a_partial_write(self) -> None:
        item_id = self.ordered_and_received("Impossible observation")
        before = {
            path.name: path.read_bytes()
            for path in sorted((self.root / "Data" / "store").glob("*.jsonl"))
        }
        error = self.failed(
            "sell",
            "--actor",
            "Lifecycle semantics test",
            "--source-ref",
            "impossible historical observation",
            "--item-id",
            item_id,
            "--sold-on",
            "2025-01-02",
            "--observed-on",
            "2024-12-31",
        )
        self.assertIn("cannot be observed before it occurred", error["error"])
        after = {
            path.name: path.read_bytes()
            for path in sorted((self.root / "Data" / "store").glob("*.jsonl"))
        }
        self.assertEqual(after, before)

    def test_reacquisition_clears_prior_episode_facts_and_requires_current_quantity(self) -> None:
        item_id = self.ordered_and_received("Reacquired fixture", quantity="2")
        self.sell(item_id)
        missing_quantity = self.failed(
            "restore-current-ownership",
            "--actor",
            "Lifecycle semantics test",
            "--source-ref",
            "physical reacquisition; function and count not checked",
            "--item-id",
            item_id,
            "--checked-on",
            "2026-08-07",
            "--location-id",
            "loc-test",
            "--reason",
            "reacquired",
        )
        self.assertIn("requires --quantity", missing_quantity["error"])

        restored = self.cli(
            "restore-current-ownership",
            "--actor",
            "Lifecycle semantics test",
            "--source-ref",
            "physical reacquisition; function not checked",
            "--item-id",
            item_id,
            "--checked-on",
            "2026-08-07",
            "--location-id",
            "loc-test",
            "--quantity",
            "1",
            "--reason",
            "reacquired",
        )["result"]
        self.assertEqual(restored["item_id"], item_id)
        shown = self.cli("show", item_id)
        item = shown["item"]
        self.assertEqual(item["ownership_state"], "confirmed")
        self.assertEqual(item["quantity"], 1)
        self.assertIsNone(item["condition"])
        self.assertIsNone(item["acquired_on"])
        self.assertIsNone(item["purchase_price"])
        self.assertIsNone(item["purchase_currency"])
        self.assertIsNone(item["receipt_ref"])
        self.assertEqual(item["serial_or_lot"], "serial-Reacquired fixture")
        amendment = shown["item_detail_amendments"][-1]
        predecessor = json.loads(amendment["previous_json"])
        changes = json.loads(amendment["changes_json"])
        self.assertEqual(predecessor["condition"], "working")
        self.assertEqual(predecessor["acquired_on"], "2020-01-02")
        self.assertEqual(predecessor["purchase_price"], 42)
        self.assertEqual(changes["condition"], None)
        self.assertEqual(changes["acquired_on"], None)
        self.assertEqual(changes["purchase_price"], None)
        self.assertEqual(changes["purchase_currency"], None)
        self.assertEqual(changes["receipt_ref"], None)

        mate = self.cli(
            "discover",
            "--actor",
            "Lifecycle semantics test",
            "--source-ref",
            "current physical check of compatible mate",
            "--name",
            "Compatible mate",
            "--category",
            "test fixture",
            "--checked-on",
            "2026-08-07",
            "--location-id",
            "loc-test",
            "--new-model",
            "--new-unit",
            "--condition",
            "working",
            "--sensitivity",
            "low",
        )["result"]
        self.cli(
            "relate",
            "--source-ref",
            "physical compatibility check after reacquisition",
            "--subject-item-id",
            item_id,
            "--object-item-id",
            mate["item_id"],
            "--predicate",
            "works_with",
            "--confidence",
            "verified",
            "--captured-on",
            "2026-08-07",
            "--evidence-type",
            "physical_check",
            "--claim-strength",
            "explicit_current",
        )
        compatibility = self.cli("compatibility", item_id, mate["item_id"])
        self.assertEqual(compatibility["outcome"], "compatible")
        self.assertEqual(compatibility["operational_outcome"], "unknown")
        self.assertIsNone(compatibility["availability"]["first"]["available"])
        insurance = self.cli("insurance-status")
        insurance_item = next(
            row for row in insurance["items"] if row["item_id"] == item_id
        )
        self.assertEqual(
            insurance_item["fields"]["acquired_date"], {"state": "unknown"}
        )
        self.assertEqual(self.cli("status")["status"], "pass")

    def test_rebuild_rejects_an_exact_event_observed_before_occurrence(self) -> None:
        item_id = self.ordered_and_received("Tampered observation")
        self.sell(item_id)
        event_store = self.root / "Data" / "store" / "inventory_events.jsonl"
        rows = [json.loads(line) for line in event_store.read_text().splitlines()]
        sold = next(row for row in rows if row["event_type"] == "sold")
        sold["observed_on"] = "2024-12-31"
        event_store.write_text(
            "".join(json.dumps(row, sort_keys=True) + "\n" for row in rows)
        )
        error = self.failed("status")
        self.assertIn("observed before it occurred", error["error"])

    def test_ownership_correction_preserves_valid_prior_episode_facts(self) -> None:
        item_id = self.ordered_and_received("Correction fixture")
        self.sell(item_id)
        self.cli(
            "restore-current-ownership",
            "--actor",
            "Lifecycle semantics test",
            "--source-ref",
            "physical check proved the sale record was wrong",
            "--item-id",
            item_id,
            "--checked-on",
            "2026-08-07",
            "--location-id",
            "loc-test",
            "--reason",
            "ownership_corrected",
        )
        item = self.cli("show", item_id)["item"]
        self.assertEqual(item["condition"], "working")
        self.assertEqual(item["acquired_on"], "2020-01-02")
        self.assertEqual(item["purchase_price"], 42)
        self.assertEqual(item["purchase_currency"], "GBP")
        self.assertEqual(item["receipt_ref"], "old receipt for Correction fixture")

    def test_reacquisition_reaffirms_unchanged_condition_and_quantity(self) -> None:
        item_id = self.ordered_and_received("Reaffirmed fixture", quantity="2")
        self.sell(item_id)
        restored = self.cli(
            "restore-current-ownership",
            "--actor",
            "Lifecycle semantics test",
            "--source-ref",
            "current count and function physically rechecked",
            "--item-id",
            item_id,
            "--checked-on",
            "2026-08-07",
            "--location-id",
            "loc-test",
            "--quantity",
            "2",
            "--condition",
            "working",
            "--reason",
            "reacquired",
        )["result"]
        self.assertIsNotNone(restored["quantity_event_id"])
        self.assertEqual(len(restored["detail_amendment_ids"]), 2)
        shown = self.cli("show", item_id)
        quantity_event = next(
            event
            for event in shown["events"]
            if event["event_id"] == restored["quantity_event_id"]
        )
        self.assertEqual(
            json.loads(quantity_event["details_json"]),
            {
                "previous_quantity": 2,
                "previous_unit": "item",
                "quantity": 2,
                "unit": "item",
            },
        )
        reset, reaffirmation = shown["item_detail_amendments"][-2:]
        self.assertIsNone(json.loads(reset["changes_json"])["condition"])
        self.assertEqual(
            json.loads(reaffirmation["changes_json"]), {"condition": "working"}
        )
        self.assertEqual(shown["item"]["condition"], "working")
        self.assertEqual(self.cli("status")["status"], "pass")

    def test_rebuild_rejects_a_pre_fix_reacquisition_with_stale_episode_facts(self) -> None:
        item_id = self.ordered_and_received("Unsafe old reacquisition")
        self.sell(item_id)
        restored = self.cli(
            "restore-current-ownership",
            "--actor",
            "Lifecycle semantics test",
            "--source-ref",
            "physical reacquisition without a function check",
            "--item-id",
            item_id,
            "--checked-on",
            "2026-08-07",
            "--location-id",
            "loc-test",
            "--reason",
            "reacquired",
        )["result"]
        amendment_store = (
            self.root / "Data" / "store" / "item_detail_amendments.jsonl"
        )
        amendments = [
            json.loads(line) for line in amendment_store.read_text().splitlines()
        ]
        reset = next(
            row
            for row in amendments
            if row["detail_amendment_id"] == restored["detail_amendment_id"]
        )
        for row in amendments:
            if (
                row["item_id"] == item_id
                and row["detail_amendment_id"] != reset["detail_amendment_id"]
            ):
                row["amended_on"] = "2026-08-07"
        amendment_store.write_text(
            "".join(
                json.dumps(row, sort_keys=True) + "\n"
                for row in amendments
                if row["detail_amendment_id"] != reset["detail_amendment_id"]
            )
        )
        items_store = self.root / "Data" / "store" / "items.jsonl"
        items = [json.loads(line) for line in items_store.read_text().splitlines()]
        predecessor = json.loads(reset["previous_json"])
        for item in items:
            if item["item_id"] == item_id:
                item.update(predecessor)
        items_store.write_text(
            "".join(json.dumps(item, sort_keys=True) + "\n" for item in items)
        )
        error = self.failed("status")
        self.assertIn("lacks an ownership-episode reset artifact", error["error"])


if __name__ == "__main__":
    unittest.main()
