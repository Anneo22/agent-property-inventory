"""Black-box CLI acceptance tests for the schema-v6 agent workflows.

These tests use only the installed command surface.  They deliberately cover
the facts an agent must be able to rely on, rather than unit-testing helpers.
"""

from __future__ import annotations

import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

HERE = Path(__file__).resolve().parents[1]
CLI = HERE / "property_inventory.py"
TODAY = "2026-08-06"


class V6AgentWorkflowTest(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory(prefix="inventory-v6-agent-")
        self.scratch = Path(self.temp.name)
        self.root = self.scratch / "inventory"
        self.runtime = self.scratch / "runtime"
        self.cli("init")
        self.add_location("loc-home", "Home", "place", "low")
        self.add_location("loc-workshop", "Workshop", "room", "low", "loc-home")

    def tearDown(self) -> None:
        self.temp.cleanup()

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
            self.command(*arguments, scope=scope), text=True, capture_output=True, check=False
        )
        self.assertEqual(completed.returncode, 0, completed.stderr or completed.stdout)
        return json.loads(completed.stdout)

    def cli_fails(self, *arguments: str, scope: str = "private") -> dict:
        completed = subprocess.run(
            self.command(*arguments, scope=scope), text=True, capture_output=True, check=False
        )
        self.assertNotEqual(completed.returncode, 0, completed.stdout)
        return json.loads(completed.stderr)

    def add_location(
        self,
        location_id: str,
        name: str,
        kind: str,
        sensitivity: str,
        parent_location_id: str | None = None,
    ) -> None:
        arguments = [
            "add-location",
            "--location-id",
            location_id,
            "--name",
            name,
            "--kind",
            kind,
            "--sensitivity",
            sensitivity,
        ]
        if parent_location_id is not None:
            arguments.extend(("--parent-location-id", parent_location_id))
        self.cli(*arguments)

    def discover(
        self,
        name: str,
        *,
        location_id: str = "loc-workshop",
        sensitivity: str = "low",
        condition: str | None = "working",
    ) -> dict:
        arguments = [
            "discover",
            "--actor",
            "V6 workflow test",
            "--source-ref",
            f"Physical check of {name}",
            "--name",
            name,
            "--category",
            "tool",
            "--checked-on",
            TODAY,
            "--location-id",
            location_id,
            "--new-model",
            "--sensitivity",
            sensitivity,
        ]
        if condition is not None:
            arguments.extend(("--condition", condition))
        return self.cli(*arguments)["result"]

    def show(self, item_id: str, *, scope: str = "private") -> dict:
        return self.cli("show", item_id, scope=scope)

    def test_evidence_backed_tags_are_scope_safe_and_searchable(self) -> None:
        item = self.discover("Taggable wrench")
        evidence_id = item["evidence_id"]
        self.cli(
            "add-tag",
            "--item-id",
            item["item_id"],
            "--tag",
            "bike-tool",
            "--evidence-id",
            evidence_id,
            "--sensitivity",
            "low",
        )
        result = self.cli("search", "bike-tool", scope="public")
        self.assertEqual([match["item"]["item_id"] for match in result["matches"]], [item["item_id"]])
        tag = result["matches"][0]["item_tags"][0]
        self.assertEqual(tag["tag"], "bike-tool")
        self.assertIsNone(tag["evidence_id"])
        invalid = self.cli_fails(
            "add-tag",
            "--item-id",
            item["item_id"],
            "--tag",
            "Bad Tag",
            "--evidence-id",
            evidence_id,
        )
        self.assertEqual(invalid["status"], "error")

    def test_durable_dimensions_drive_fit_and_pack(self) -> None:
        self.add_location("loc-box", "Tool box", "container", "low", "loc-workshop")
        self.cli(
            "add-space",
            "--location-id",
            "loc-box",
            "--source-ref",
            "Measured box",
            "--captured-on",
            TODAY,
            "--sensitivity",
            "low",
            "--profile",
            json.dumps({"kind": "container_box", "x": 0, "y": 0, "z": 0, "width": 30, "height": 20, "depth": 10, "unit": "cm"}),
        )
        first = self.discover("Measured pump")
        second = self.discover("Measured lever")
        for item, width in ((first, 20), (second, 5)):
            self.cli(
                "add-item-dimensions",
                "--item-id",
                item["item_id"],
                "--width",
                str(width),
                "--height",
                "5",
                "--depth",
                "5",
                "--unit",
                "cm",
                "--measured-on",
                TODAY,
                "--evidence-id",
                item["evidence_id"],
                "--sensitivity",
                "low",
            )
        fit = self.cli("fit", "loc-box", "--item-id", first["item_id"])
        self.assertEqual(fit["status"], "fits")
        packed = self.cli(
            "pack", "loc-box", "--item-id", first["item_id"], "--item-id", second["item_id"]
        )
        self.assertEqual(packed["status"], "packed")
        self.assertEqual({row["item_id"] for row in packed["placements"]}, {first["item_id"], second["item_id"]})
        free = self.cli(
            "free-volume",
            "loc-box",
            "--occupied-box",
            json.dumps(
                {
                    "x": 0,
                    "y": 0,
                    "z": 0,
                    "width": 10,
                    "height": 5,
                    "depth": 5,
                    "unit": "cm",
                }
            ),
        )
        self.assertEqual(free["status"], "known")
        self.assertEqual(free["free_volume"], 5750.0)
        self.assertEqual(free["unit"], "cm")
        self.cli(
            "change",
            "--actor", "V6 workflow test",
            "--source-ref", "Pump lent away",
            "--item-id", first["item_id"],
            "--event-type", "lent",
            "--occurred-on", "2026-08-07",
        )
        unavailable_fit = self.cli("fit", "loc-box", "--item-id", first["item_id"])
        self.assertEqual(unavailable_fit["status"], "unknown")
        self.assertEqual(unavailable_fit["reason"], "item_not_operationally_available")
        unavailable_pack = self.cli(
            "pack", "loc-box", "--item-id", first["item_id"], "--item-id", second["item_id"]
        )
        self.assertEqual(unavailable_pack["status"], "unknown")
        self.assertFalse(unavailable_pack["unavailable_items"][0]["availability"]["available"])

    def test_dimensions_select_the_latest_visible_axis_before_scope_comparison(self) -> None:
        self.add_location("loc-scope-box", "Scope box", "container", "low", "loc-workshop")
        self.cli(
            "add-space",
            "--location-id",
            "loc-scope-box",
            "--source-ref",
            "Measured public scope box",
            "--captured-on",
            TODAY,
            "--sensitivity",
            "low",
            "--profile",
            json.dumps(
                {
                    "kind": "container_box",
                    "x": 0,
                    "y": 0,
                    "z": 0,
                    "width": 30,
                    "height": 10,
                    "depth": 10,
                    "unit": "cm",
                }
            ),
        )
        item = self.discover("Scope-measured item")
        self.cli(
            "add-item-dimensions",
            "--item-id",
            item["item_id"],
            "--width",
            "20",
            "--height",
            "5",
            "--depth",
            "5",
            "--unit",
            "cm",
            "--measured-on",
            TODAY,
            "--evidence-id",
            item["evidence_id"],
            "--sensitivity",
            "low",
        )
        hidden_evidence = self.cli(
            "record-evidence",
            "--item-id",
            item["item_id"],
            "--source-ref",
            "Restricted later measurement",
            "--captured-on",
            "2026-08-07",
            "--evidence-type",
            "user_source",
            "--claim-strength",
            "research_only",
            "--sensitivity",
            "high",
        )["result"]["evidence_id"]
        self.cli(
            "add-item-dimensions",
            "--item-id",
            item["item_id"],
            "--width",
            "40",
            "--height",
            "5",
            "--depth",
            "5",
            "--unit",
            "cm",
            "--measured-on",
            "2026-08-07",
            "--evidence-id",
            hidden_evidence,
            "--sensitivity",
            "high",
        )

        public_fit = self.cli("fit", "loc-scope-box", "--item-id", item["item_id"], scope="public")
        private_fit = self.cli("fit", "loc-scope-box", "--item-id", item["item_id"])
        self.assertEqual(public_fit["status"], "fits")
        self.assertEqual(private_fit["status"], "does_not_fit")
        self.assertNotIn("restricted", json.dumps(public_fit).casefold())

    def test_identity_correction_keeps_one_item_and_exact_retry(self) -> None:
        item = self.discover("Wrongly named driver")
        correction = (
            "correct-item-identity",
            "--actor", "V6 workflow test",
            "--item-id", item["item_id"],
            "--evidence-id", item["evidence_id"],
            "--amended-on", TODAY,
            "--reason", "identity_correction",
            "--name", "Correctly named driver",
            "--category", "tool",
            "--new-model",
        )
        first = self.cli(*correction)["result"]
        second = self.cli(*correction)["result"]
        self.assertFalse(first["reused"])
        self.assertTrue(second["reused"])
        shown = self.show(item["item_id"])
        self.assertEqual(shown["item"]["model_id"], first["target_model_id"])
        self.assertEqual(len(shown["item_amendments"]), 1)
        self.assertEqual(
            self.cli("list-items", "--limit", "500")["count"], 1
        )

    def test_sale_then_restore_reuses_the_same_item_with_physical_evidence(self) -> None:
        item = self.discover("Recovered wheel")
        self.cli(
            "sell",
            "--actor",
            "V6 workflow test",
            "--item-id",
            item["item_id"],
            "--sold-on",
            TODAY,
            "--source-ref",
            "Sold by mistake",
        )
        restored = self.cli(
            "restore-current-ownership",
            "--actor",
            "V6 workflow test",
            "--item-id",
            item["item_id"],
            "--checked-on",
            "2026-08-07",
            "--location-id",
            "loc-workshop",
            "--reason",
            "ownership_corrected",
            "--source-ref",
            "Physical check: wheel still present",
        )["result"]
        self.assertEqual(restored["item_id"], item["item_id"])
        shown = self.show(item["item_id"])
        self.assertEqual(shown["item"]["ownership_state"], "confirmed")
        self.assertEqual(shown["item"]["verified_on"], "2026-08-07")
        self.assertEqual(
            [event["event_type"] for event in shown["events"]][-2:],
            ["ownership_corrected", "physically_verified"],
        )
        self.assertEqual(self.cli("list-items", "--limit", "500")["count"], 1)

    def test_area_enumeration_is_complete_at_scope_and_does_not_leak_canaries(self) -> None:
        self.add_location("loc-cupboard", "Cupboard", "container", "low", "loc-workshop")
        self.add_location("loc-garage", "Garage", "room", "low", "loc-home")
        self.add_location("loc-secret", "CANARY secret room", "room", "high", "loc-home")
        low_item = self.discover("Visible allen keys", location_id="loc-cupboard")
        self.discover("CANARY secret item", location_id="loc-secret", sensitivity="high")
        listed = self.cli("list-items", "--location", "loc-home", "--limit", "1", scope="public")
        self.assertEqual(listed["count"], 1)
        self.assertEqual(listed["matches"][0]["item"]["item_id"], low_item["item_id"])
        self.assertNotIn("CANARY", json.dumps(listed, sort_keys=True))
        locations = self.cli("locations", "--parent-location-id", "loc-home", "--limit", "1", scope="public")
        self.assertEqual(locations["count"], 2)
        self.assertEqual(len(locations["matches"]), 1)
        self.assertEqual(locations["matches"][0]["location_id"], "loc-garage")
        self.assertTrue(locations["truncated"])
        self.assertNotIn("CANARY", json.dumps(locations, sort_keys=True))
        self.assertEqual(locations["meaning_if_empty"], "unknown, not absent")

    def test_kit_possession_rejects_purchase_evidence_and_reports_verified_ready(self) -> None:
        served = self.discover("Bike")
        component = self.cli(
            "order",
            "--actor", "V6 workflow test",
            "--source-ref", "Purchase order for tyre lever",
            "--name", "Tyre lever",
            "--category", "tool",
            "--ordered-on", TODAY,
            "--order-placed",
            "--new-model",
            "--sensitivity", "low",
        )["result"]
        kit = self.cli(
            "add-kit",
            "--kit-id", "kit-bike",
            "--name", "Bike roadside kit",
            "--serves-item-id", served["item_id"],
            "--evidence-id", served["evidence_id"],
        )["result"]
        self.assertIn(component["evidence_id"], self.show(component["item_id"])["evidence_ids"])
        purchase_only = self.cli_fails(
            "set-kit-requirement",
            "--actor", "V6 workflow test",
            "--kit-id", kit["kit_id"],
            "--requirement-key", "lever",
            "--item-id", component["item_id"],
            "--status", "source_present",
            "--evidence-id", component["evidence_id"],
        )
        self.assertEqual(purchase_only["status"], "error")
        component_check = self.cli(
            "physical-check",
            "--actor", "V6 workflow test",
            "--item-id", component["item_id"],
            "--checked-on", TODAY,
            "--location-id", "loc-workshop",
            "--source-ref", "Physical check of delivered tyre lever",
            "--condition", "working",
        )["result"]
        self.cli(
            "set-kit-requirement",
            "--actor", "V6 workflow test",
            "--kit-id", kit["kit_id"],
            "--requirement-key", "lever",
            "--item-id", component["item_id"],
            "--status", "source_present",
            "--evidence-id", component_check["evidence_id"],
        )
        self.cli(
            "review-kit",
            "--actor", "V6 workflow test",
            "--kit-id", kit["kit_id"],
            "--reviewed-on", TODAY,
            "--completeness", "complete",
            "--source-ref", "Reviewed the complete roadside-kit requirement list",
        )
        status = self.cli("kit-status", "--kit-id", kit["kit_id"])
        self.assertEqual(status["kits"][0]["readiness"], "verified_ready")

    def test_torque_decisions_never_treat_missing_or_out_of_range_limits_as_safe(self) -> None:
        tool = self.discover("Torque wrench")
        self.cli_fails(
            "add-torque-path",
            "--tool-item-id", tool["item_id"],
            "--output-drive", "1/4 inch",
            "--status", "direct",
            "--evidence-id", tool["evidence_id"],
        )
        self.cli(
            "add-torque-path",
            "--tool-item-id", tool["item_id"],
            "--output-drive", "1/4 inch",
            "--min-torque-nm", "2",
            "--max-torque-nm", "10",
            "--status", "direct",
            "--evidence-id", tool["evidence_id"],
            "--path-id", "torque-direct",
        )
        safe = self.cli("torque-check", "--path-id", "torque-direct", "--requested-nm", "10")
        unsafe = self.cli("torque-check", "--path-id", "torque-direct", "--requested-nm", "10.1")
        self.assertEqual(safe["decisions"][0]["outcome"], "safe")
        self.assertEqual(unsafe["decisions"][0]["outcome"], "unsafe")
        self.cli(
            "change",
            "--actor", "V6 workflow test",
            "--source-ref", "Torque wrench lent away",
            "--item-id", tool["item_id"],
            "--event-type", "lent",
            "--occurred-on", "2026-08-07",
        )
        unavailable = self.cli(
            "torque-check", "--path-id", "torque-direct", "--requested-nm", "5"
        )["decisions"][0]
        self.assertEqual(unavailable["physical_outcome"], "safe")
        self.assertEqual(unavailable["outcome"], "unknown")
        self.assertFalse(unavailable["availability"]["available"])

    def test_compatibility_separates_physical_fact_from_current_availability(self) -> None:
        first = self.discover("Compatible tool")
        second = self.discover("Compatible attachment")
        self.cli(
            "relate",
            "--source-ref", "Physical compatibility check",
            "--subject-item-id", first["item_id"],
            "--object-item-id", second["item_id"],
            "--predicate", "works_with",
            "--confidence", "verified",
            "--captured-on", TODAY,
            "--evidence-type", "physical_check",
            "--claim-strength", "explicit_current",
        )
        current = self.cli("compatibility", first["item_id"], second["item_id"])
        self.assertEqual(current["outcome"], "compatible")
        self.assertEqual(current["operational_outcome"], "compatible")
        self.cli(
            "sell",
            "--actor", "V6 workflow test",
            "--source-ref", "Attachment sold",
            "--item-id", second["item_id"],
            "--sold-on", "2026-08-07",
        )
        sold = self.cli("compatibility", first["item_id"], second["item_id"])
        self.assertEqual(sold["outcome"], "compatible")
        self.assertEqual(sold["operational_outcome"], "unknown")
        self.assertFalse(sold["availability"]["second"]["available"])

    def test_broken_or_unknown_condition_never_yields_operational_torque_or_compatibility_advice(self) -> None:
        for condition, expected_available in (("broken", False), (None, None)):
            with self.subTest(condition=condition):
                suffix = "broken" if condition is not None else "unknown"
                tool = self.discover(
                    f"{suffix} torque wrench", condition=condition
                )
                attachment = self.discover(f"{suffix} attachment")
                self.cli(
                    "add-torque-path",
                    "--tool-item-id", tool["item_id"],
                    "--output-drive", "1/4 inch",
                    "--min-torque-nm", "2",
                    "--max-torque-nm", "10",
                    "--status", "direct",
                    "--evidence-id", tool["evidence_id"],
                    "--path-id", f"torque-{suffix}",
                )
                torque = self.cli(
                    "torque-check", "--path-id", f"torque-{suffix}",
                    "--requested-nm", "5",
                )["decisions"][0]
                self.assertEqual(torque["physical_outcome"], "safe")
                self.assertEqual(torque["outcome"], "unknown")
                self.assertEqual(
                    torque["availability"]["available"], expected_available
                )
                self.cli(
                    "relate",
                    "--source-ref", f"Physical compatibility check, {suffix} fixture",
                    "--subject-item-id", tool["item_id"],
                    "--object-item-id", attachment["item_id"],
                    "--predicate", "works_with",
                    "--confidence", "verified",
                    "--captured-on", TODAY,
                    "--evidence-type", "physical_check",
                    "--claim-strength", "explicit_current",
                )
                compatibility = self.cli(
                    "compatibility", tool["item_id"], attachment["item_id"]
                )
                self.assertEqual(compatibility["outcome"], "compatible")
                self.assertEqual(compatibility["operational_outcome"], "unknown")
                self.assertEqual(
                    compatibility["availability"]["first"]["available"],
                    expected_available,
                )

    def test_broken_or_unknown_source_present_component_keeps_kit_readiness_unknown(self) -> None:
        for condition in ("broken", None):
            with self.subTest(condition=condition):
                suffix = "broken" if condition is not None else "unknown"
                served = self.discover(f"{suffix} kit host")
                component = self.discover(
                    f"{suffix} kit component", condition=condition
                )
                kit = self.cli(
                    "add-kit",
                    "--kit-id", f"kit-{suffix}",
                    "--name", f"{suffix} readiness kit",
                    "--serves-item-id", served["item_id"],
                    "--evidence-id", served["evidence_id"],
                )["result"]
                self.cli(
                    "set-kit-requirement",
                    "--actor", "V6 workflow test",
                    "--kit-id", kit["kit_id"],
                    "--requirement-key", "component",
                    "--item-id", component["item_id"],
                    "--status", "source_present",
                    "--evidence-id", component["evidence_id"],
                )
                self.cli(
                    "review-kit",
                    "--actor", "V6 workflow test",
                    "--kit-id", kit["kit_id"],
                    "--reviewed-on", TODAY,
                    "--completeness", "complete",
                    "--source-ref", f"Reviewed {suffix} kit requirements",
                )
                status = self.cli("kit-status", "--kit-id", kit["kit_id"])
                self.assertEqual(status["kits"][0]["readiness"], "unknown")


if __name__ == "__main__":
    unittest.main()
