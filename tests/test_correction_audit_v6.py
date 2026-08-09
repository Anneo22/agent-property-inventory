"""End-to-end proof for replayable physical and factual correction history."""

from __future__ import annotations

import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

HERE = Path(__file__).resolve().parents[1]
CLI = HERE / "property_inventory.py"


class CorrectionAuditV6Test(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory(prefix="inventory-correction-v6-")
        scratch = Path(self.temporary.name)
        self.root = scratch / "inventory"
        self.runtime = scratch / "runtime"
        self.cli("init")
        self.cli(
            "add-location",
            "--location-id",
            "loc-home",
            "--name",
            "Home",
            "--kind",
            "place",
            "--sensitivity",
            "low",
        )

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

    def cli(self, *arguments: str, scope: str = "private") -> dict:
        completed = subprocess.run(
            self.command(*arguments, scope=scope),
            text=True,
            capture_output=True,
            check=False,
        )
        self.assertEqual(completed.returncode, 0, completed.stderr or completed.stdout)
        return json.loads(completed.stdout)

    def cli_fails(self, *arguments: str, scope: str = "private") -> dict:
        completed = subprocess.run(
            self.command(*arguments, scope=scope),
            text=True,
            capture_output=True,
            check=False,
        )
        self.assertNotEqual(completed.returncode, 0, completed.stdout)
        return json.loads(completed.stderr)

    def discover(self) -> dict:
        return self.cli(
            "discover",
            "--actor",
            "correction test",
            "--source-ref",
            "overview crop",
            "--name",
            "Socket driver",
            "--category",
            "tool",
            "--checked-on",
            "2026-08-05",
            "--location-id",
            "loc-home",
            "--new-model",
            "--quantity",
            "1",
            "--condition",
            "used",
            "--serial-or-lot",
            "SERIAL-A",
            "--sensitivity",
            "low",
        )["result"]

    def test_physical_check_and_enrichment_preserve_predecessors(self) -> None:
        discovered = self.discover()
        item_id = discovered["item_id"]
        checked = self.cli(
            "physical-check",
            "--actor",
            "correction test",
            "--source-ref",
            "close-up crop",
            "--item-id",
            item_id,
            "--checked-on",
            "2026-08-06",
            "--location-unchanged",
            "--quantity",
            "2",
            "--condition",
            "good",
            "--serial-or-lot",
            "SERIAL-B",
        )["result"]
        self.assertIsNotNone(checked["quantity_event_id"])
        self.assertIsNotNone(checked["detail_amendment_id"])

        shown = self.cli("show", item_id)
        quantity_event = next(
            event for event in shown["events"] if event["event_type"] == "quantity_changed"
        )
        self.assertEqual(
            json.loads(quantity_event["details_json"]),
            {
                "previous_quantity": 1.0,
                "previous_unit": "item",
                "quantity": 2.0,
                "unit": "item",
            },
        )
        physical_amendment = shown["item_detail_amendments"][0]
        self.assertEqual(json.loads(physical_amendment["previous_json"])["condition"], "used")
        self.assertEqual(json.loads(physical_amendment["changes_json"])["serial_or_lot"], "SERIAL-B")

        receipt = self.cli(
            "record-evidence",
            "--item-id",
            item_id,
            "--source-ref",
            "receipt-001",
            "--captured-on",
            "2026-08-07",
            "--evidence-type",
            "user_source",
            "--claim-strength",
            "purchase_only",
            "--sensitivity",
            "low",
        )["result"]
        enrichment = (
            "enrich-item",
            "--actor",
            "correction test",
            "--item-id",
            item_id,
            "--evidence-id",
            receipt["evidence_id"],
            "--amended-on",
            "2026-08-07",
            "--acquired-on",
            "2025-01-02",
            "--purchase-price",
            "19.99",
            "--purchase-currency",
            "GBP",
            "--receipt-ref",
            "receipt-001",
        )
        first = self.cli(*enrichment)["result"]
        second = self.cli(*enrichment)["result"]
        self.assertFalse(first["reused"])
        self.assertTrue(second["reused"])
        shown = self.cli("show", item_id)
        self.assertEqual(shown["item"]["receipt_ref"], "receipt-001")
        self.assertEqual(len(shown["item_detail_amendments"]), 2)
        public = self.cli("show", item_id, scope="public")
        self.assertEqual(public["item_detail_amendments"], [])

    def test_fact_replace_retract_is_visible_and_tampering_fails_closed(self) -> None:
        discovered = self.discover()
        item_id = discovered["item_id"]
        alias = self.cli(
            "add-alias",
            "--item-id",
            item_id,
            "--alias",
            "old alias",
            "--alias-kind",
            "nickname",
            "--evidence-id",
            discovered["evidence_id"],
            "--sensitivity",
            "low",
        )["result"]
        correction = self.cli(
            "record-evidence",
            "--item-id",
            item_id,
            "--source-ref",
            "manual label check",
            "--captured-on",
            "2026-08-06",
            "--evidence-type",
            "user_source",
            "--claim-strength",
            "research_only",
            "--sensitivity",
            "low",
        )["result"]
        selector = {"alias_id": alias["alias_id"]}
        replacement = {
            "alias_id": alias["alias_id"],
            "item_id": item_id,
            "alias": "correct alias",
            "alias_kind": "nickname",
            "evidence_id": correction["evidence_id"],
            "sensitivity": "low",
            "notes": None,
        }
        self.cli(
            "amend-fact",
            "--actor",
            "correction test",
            "--table",
            "aliases",
            "--selector",
            json.dumps(selector),
            "--action",
            "replace",
            "--replacement",
            json.dumps(replacement),
            "--evidence-id",
            correction["evidence_id"],
            "--amended-on",
            "2026-08-06",
            "--reason",
            "label correction",
        )
        self.cli(
            "amend-fact",
            "--actor",
            "correction test",
            "--table",
            "aliases",
            "--selector",
            json.dumps(selector),
            "--action",
            "retract",
            "--evidence-id",
            correction["evidence_id"],
            "--amended-on",
            "2026-08-07",
            "--reason",
            "alias not useful",
        )
        shown = self.cli("show", item_id)
        self.assertEqual(len(shown["fact_amendments"]), 2)
        self.assertEqual(shown["fact_amendments"][-1]["action"], "retract")
        self.assertFalse(self.cli("search", "correct alias")["recorded"])
        self.assertEqual(
            self.cli("show", item_id, scope="public")["fact_amendments"], []
        )

        path = self.root / "Data" / "store" / "fact_amendments.jsonl"
        rows = [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()]
        rows[-1]["previous_json"] = rows[0]["previous_json"]
        path.write_text(
            "".join(json.dumps(row, sort_keys=True) + "\n" for row in rows),
            encoding="utf-8",
        )
        failure = self.cli_fails("status")
        self.assertEqual(failure["status"], "error")


if __name__ == "__main__":
    unittest.main()
