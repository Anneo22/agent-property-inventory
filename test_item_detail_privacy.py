"""Regression contract for scope-safe materialized item details."""

from __future__ import annotations

import asyncio
import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

from mcp.client import Client
from mcp.client.stdio import StdioServerParameters, stdio_client

HERE = Path(__file__).resolve().parent
CLI = HERE / "property_inventory.py"
MCP = Path(sys.executable).parent / "property-inventory-mcp"
HIGH_CONDITION = "working"
HIGH_ACQUIRED_ON = "1999-09-09"
HIGH_SERIAL = "CANARY-HIGH-SERIAL"


class ItemDetailPrivacyTest(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory(prefix="inventory-detail-privacy-")
        self.scratch = Path(self.temp.name)
        self.root = self.scratch / "inventory"
        self.runtime = self.scratch / "runtime"
        self.catalogue = self.scratch / "Inventory.md"
        self.cli("init")
        self.cli(
            "add-location",
            "--location-id", "loc-public-workshop",
            "--name", "Public workshop",
            "--kind", "room",
            "--sensitivity", "low",
        )
        ordered = self.cli(
            "order",
            "--actor", "Privacy fixture",
            "--source-ref", "Public order fixture",
            "--name", "public detail fixture",
            "--category", "test fixture",
            "--ordered-on", "2026-08-06",
            "--order-placed",
            "--location-id", "loc-public-workshop",
            "--sensitivity", "low",
        )
        self.item_id = ordered["result"]["item_id"]
        received = self.cli(
            "receive",
            "--actor", "Privacy fixture",
            "--source-ref", "Public delivery fixture",
            "--item-id", self.item_id,
            "--received-on", "2026-08-06",
            "--location-unchanged",
            "--condition", "public initial condition",
            "--physical-check",
        )
        self.cli(
            "add-torque-path",
            "--tool-item-id", self.item_id,
            "--output-drive", "1/4 inch",
            "--min-torque-nm", "2",
            "--max-torque-nm", "10",
            "--status", "direct",
            "--evidence-id", received["result"]["evidence_id"],
            "--path-id", "torque-public-privacy",
        )
        mate = self.cli(
            "discover",
            "--actor", "Privacy fixture",
            "--source-ref", "Public physical mate fixture",
            "--name", "public compatibility mate",
            "--category", "test fixture",
            "--checked-on", "2026-08-06",
            "--location-id", "loc-public-workshop",
            "--new-model",
            "--new-unit",
            "--condition", "working",
            "--sensitivity", "low",
        )
        self.mate_id = mate["result"]["item_id"]
        self.cli(
            "relate",
            "--source-ref", "Public physical compatibility fixture",
            "--subject-item-id", self.item_id,
            "--object-item-id", self.mate_id,
            "--predicate", "works_with",
            "--confidence", "verified",
            "--captured-on", "2026-08-06",
            "--evidence-type", "physical_check",
            "--claim-strength", "explicit_current",
        )
        kit = self.cli(
            "add-kit",
            "--name", "Public privacy kit",
            "--serves-item-id", self.mate_id,
            "--evidence-id", mate["result"]["evidence_id"],
            "--kit-id", "kit-public-privacy",
        )["result"]
        self.cli(
            "set-kit-requirement",
            "--actor", "Privacy fixture",
            "--kit-id", kit["kit_id"],
            "--requirement-key", "private_condition_tool",
            "--item-id", self.item_id,
            "--status", "source_present",
            "--evidence-id", received["result"]["evidence_id"],
        )
        self.cli(
            "review-kit",
            "--actor", "Privacy fixture",
            "--kit-id", kit["kit_id"],
            "--reviewed-on", "2026-08-06",
            "--completeness", "complete",
            "--source-ref", "Reviewed complete public privacy kit",
        )
        evidence = self.cli(
            "record-evidence",
            "--item-id", self.item_id,
            "--source-ref", "High evidence fixture",
            "--captured-on", "2026-08-06",
            "--evidence-type", "user_source",
            "--claim-strength", "claimed_owned",
            "--sensitivity", "high",
        )
        self.cli(
            "enrich-item",
            "--actor", "Privacy fixture",
            "--item-id", self.item_id,
            "--evidence-id", evidence["result"]["evidence_id"],
            "--amended-on", "2026-08-06",
            "--condition", HIGH_CONDITION,
            "--acquired-on", HIGH_ACQUIRED_ON,
            "--serial-or-lot", HIGH_SERIAL,
        )

    def tearDown(self) -> None:
        self.temp.cleanup()

    def command(self, *arguments: str, scope: str = "private") -> list[str]:
        return [
            sys.executable,
            str(CLI),
            "--inventory-root", str(self.root),
            "--runtime-dir", str(self.runtime),
            "--catalogue-output", str(self.catalogue),
            "--catalogue-scope", "public",
            "--scope", scope,
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

    def parameters(self) -> StdioServerParameters:
        self.assertTrue(MCP.is_file(), f"MCP console script not installed: {MCP}")
        return StdioServerParameters(
            command=str(MCP),
            args=[
                "--inventory-root", str(self.root),
                "--runtime-dir", str(self.runtime),
                "--catalogue-output", str(self.catalogue),
                "--catalogue-scope", "public",
                "--scope", "public",
                "--profile", "read",
            ],
            cwd=self.scratch,
        )

    def test_public_cli_hides_high_evidence_materialized_condition(self) -> None:
        self.assertEqual(
            self.cli("show", self.item_id)["item"]["condition"], HIGH_CONDITION
        )
        shown = self.cli("show", self.item_id, scope="public")
        self.assertIsNone(shown["item"]["condition"])
        self.assertIsNone(shown["item"]["acquired_on"])
        self.assertIsNone(shown["item"]["serial_or_lot"])
        self.assertNotIn(HIGH_CONDITION, json.dumps(shown, sort_keys=True))
        filtered = self.cli(
            "search", "public detail fixture", "--condition", HIGH_CONDITION, scope="public"
        )
        self.assertEqual(filtered["count"], 0)
        compatibility = self.cli(
            "compatibility", self.item_id, self.mate_id, scope="public"
        )
        self.assertIsNone(compatibility["availability"]["first"]["condition"])
        self.assertEqual(compatibility["operational_outcome"], "unknown")
        self.assertNotIn("CANARY", json.dumps(compatibility, sort_keys=True))
        torque = self.cli(
            "torque-check",
            "--path-id", "torque-public-privacy",
            "--requested-nm", "5",
            scope="public",
        )
        self.assertIsNone(torque["decisions"][0]["availability"]["condition"])
        self.assertEqual(torque["decisions"][0]["outcome"], "unknown")
        self.assertNotIn("CANARY", json.dumps(torque, sort_keys=True))
        insurance = self.cli("insurance-status", scope="public")
        item = next(row for row in insurance["items"] if row["item_id"] == self.item_id)
        self.assertEqual(item["fields"]["acquired_date"], {"state": "unknown"})
        self.assertEqual(item["fields"]["serial"], {"state": "unknown"})
        self.assertNotIn("CANARY", json.dumps(insurance, sort_keys=True))
        catalogue_row = next(
            line
            for line in self.catalogue.read_text().splitlines()
            if "private condition tool" in line.casefold()
        )
        self.assertIn("Unknown", catalogue_row)
        self.assertNotIn("source present", catalogue_row.casefold())

    def test_public_mcp_hides_high_evidence_materialized_condition(self) -> None:
        async def scenario() -> None:
            async with Client(stdio_client(self.parameters()), mode="legacy") as client:
                shown = await client.call_tool("get_inventory_item", {"item_id": self.item_id})
                filtered = await client.call_tool(
                    "search_inventory",
                    {"query": "public detail fixture", "condition": HIGH_CONDITION},
                )
                compatibility = await client.call_tool(
                    "check_compatibility",
                    {
                        "first_item_id": self.item_id,
                        "second_item_id": self.mate_id,
                    },
                )
                insurance = await client.call_tool("get_insurance_readiness", {})
                self.assertFalse(shown.is_error, shown.content)
                self.assertFalse(filtered.is_error, filtered.content)
                self.assertFalse(compatibility.is_error, compatibility.content)
                self.assertFalse(insurance.is_error, insurance.content)
                self.assertIsNone(shown.structured_content["item"]["condition"])
                self.assertNotIn(
                    HIGH_CONDITION, json.dumps(shown.structured_content, sort_keys=True)
                )
                self.assertEqual(filtered.structured_content["count"], 0)
                self.assertIsNone(
                    compatibility.structured_content["availability"]["first"]["condition"]
                )
                self.assertEqual(
                    compatibility.structured_content["operational_outcome"], "unknown"
                )
                insurance_item = next(
                    row
                    for row in insurance.structured_content["items"]
                    if row["item_id"] == self.item_id
                )
                self.assertEqual(
                    insurance_item["fields"]["acquired_date"], {"state": "unknown"}
                )
                self.assertEqual(
                    insurance_item["fields"]["serial"], {"state": "unknown"}
                )
                self.assertNotIn(
                    "CANARY", json.dumps(compatibility.structured_content, sort_keys=True)
                )
                self.assertNotIn(
                    "CANARY", json.dumps(insurance.structured_content, sort_keys=True)
                )

        asyncio.run(scenario())


if __name__ == "__main__":
    unittest.main()
