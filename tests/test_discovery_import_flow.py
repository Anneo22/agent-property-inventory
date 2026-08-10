#!/usr/bin/env python3
"""Focused end-to-end coverage for physical discovery and distinct-unit imports."""

from __future__ import annotations

import asyncio
import hashlib
import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

from mcp.client import Client
from mcp.client.stdio import StdioServerParameters, stdio_client

HERE = Path(__file__).resolve().parents[1]
CLI = HERE / "property_inventory.py"
MCP = Path(sys.executable).parent / "property-inventory-mcp"


class DiscoveryImportFlowTest(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory(prefix="inventory-discovery-import-")
        self.scratch = Path(self.temporary.name)
        self.root = self.scratch / "inventory"
        self.runtime = self.scratch / "runtime"
        self.catalogue = self.scratch / "catalogue" / "Inventory.md"
        self.cli("init")
        self.cli(
            "add-location", "--name", "Workshop", "--location-id", "loc-workshop",
            "--kind", "room", "--sensitivity", "low",
        )
        self.cli(
            "add-location", "--name", "Small case", "--location-id", "loc-small-case",
            "--parent-location-id", "loc-workshop", "--kind", "container", "--sensitivity", "low",
        )

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def command(self, *arguments: str, scope: str = "private") -> list[str]:
        return [
            sys.executable, str(CLI), "--inventory-root", str(self.root),
            "--runtime-dir", str(self.runtime), "--catalogue-output", str(self.catalogue),
            "--scope", scope, *arguments,
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

    def store_digest(self) -> str:
        digest = hashlib.sha256()
        for source in sorted((self.root / "Data" / "store").glob("*.jsonl")):
            digest.update(source.name.encode())
            digest.update(source.read_bytes())
        return digest.hexdigest()

    def discover_arguments(self, source_ref: str = "photo:tool-1") -> list[str]:
        return [
            "discover", "--actor", "physical test", "--source-ref", source_ref,
            "--name", "Checked screwdriver", "--category", "tool",
            "--checked-on", "2026-08-06", "--location-id", "loc-workshop",
            "--container-id", "loc-small-case", "--brand", "Fixture", "--model", "D-1",
        ]

    def test_discover_creates_evidence_events_and_duplicate_guard(self) -> None:
        result = self.cli(*self.discover_arguments())["result"]
        item = self.cli("show", result["item_id"])["item"]
        self.assertEqual(item["ownership_state"], "confirmed")
        self.assertEqual(item["location_id"], "loc-workshop")
        self.assertEqual(item["container_id"], "loc-small-case")
        self.assertIsNone(item["acquired_on"])
        self.assertIsNone(item["purchase_price"])
        self.assertIsNone(item["purchase_currency"])
        self.assertIsNone(item["receipt_ref"])
        evidence = next(
            json.loads(line)
            for line in (self.root / "Data" / "store" / "evidence.jsonl").read_text().splitlines()
            if json.loads(line)["evidence_id"] == result["evidence_id"]
        )
        self.assertEqual(
            (evidence["evidence_type"], evidence["claim_strength"]),
            ("physical_check", "explicit_current"),
        )
        links = [
            json.loads(line)
            for line in (self.root / "Data" / "store" / "item_evidence.jsonl").read_text().splitlines()
            if json.loads(line)["evidence_id"] == result["evidence_id"]
        ]
        self.assertEqual(links, [{"evidence_id": result["evidence_id"], "item_id": result["item_id"], "role": "primary"}])
        events = [
            json.loads(line)["event_type"]
            for line in (self.root / "Data" / "store" / "inventory_events.jsonl").read_text().splitlines()
            if json.loads(line)["item_id"] == result["item_id"]
        ]
        self.assertEqual(events, ["received", "physically_verified"])
        self.assertEqual(self.cli("status")["verification"]["failures"], [])
        self.assertIn("matching physical unit", self.fails(*self.discover_arguments())["error"])
        self.cli(
            "change", "--actor", "physical test", "--source-ref", "loan fixture",
            "--item-id", result["item_id"], "--event-type", "lent", "--occurred-on", "2026-08-06",
        )
        lent_error = self.fails(*self.discover_arguments("photo:tool-2"))["error"]
        self.assertIn(result["item_id"], lent_error)
        self.assertIn("confirmed", lent_error)
        second = self.cli(*self.discover_arguments("photo:tool-2"), "--new-unit")["result"]
        self.assertFalse(second["model_created"])
        self.assertEqual(second["model_id"], result["model_id"])

    def test_discover_is_proposal_safe_and_mcp_only_prepares(self) -> None:
        before = self.store_digest()
        operations = self.scratch / "operations.json"
        operations.write_text(json.dumps([self.discover_arguments()]), encoding="utf-8")
        prepared = self.cli("propose", "--operations", str(operations))
        self.assertEqual(prepared["status"], "prepared")
        self.assertEqual(self.store_digest(), before)
        self.cli("proposal-apply", prepared["proposal"]["proposal_id"])
        self.assertEqual(self.cli("status")["verification"]["failures"], [])

        before_mcp = self.store_digest()

        async def scenario() -> None:
            parameters = StdioServerParameters(
                command=str(MCP),
                args=[
                    "--inventory-root", str(self.root), "--runtime-dir", str(self.runtime),
                    "--catalogue-output", str(self.catalogue),
                    "--scope", "private", "--profile", "write",
                ],
                cwd=self.scratch,
            )
            async with Client(stdio_client(parameters), mode="legacy") as client:
                listed = await client.list_tools()
                names = {tool.name for tool in listed.tools}
                self.assertIn("prepare_physical_discovery", names)
                self.assertNotIn("discover", names)
                prepared_mcp = await client.call_tool(
                    "prepare_physical_discovery",
                    {
                        "actor": "MCP physical test", "source_ref": "photo:mcp-tool",
                        "name": "MCP checked tool", "category": "tool", "checked_on": "2026-08-06",
                        "location_id": "loc-workshop", "container_id": "loc-small-case",
                    },
                )
                self.assertFalse(prepared_mcp.is_error, str(prepared_mcp.content))
                self.assertEqual(prepared_mcp.structured_content["proposal"]["operations"][0][0], "discover")

        asyncio.run(scenario())
        self.assertEqual(self.store_digest(), before_mcp)

    def test_same_model_import_rows_create_distinct_candidate_units(self) -> None:
        source = self.scratch / "same-model.json"
        source.write_text(
            json.dumps(
                [
                    {"status": "ordered", "name": "Imported unit", "category": "tool", "date": "2026-08-06", "external_id": "order-a"},
                    {"status": "ordered", "name": "Imported unit", "category": "tool", "date": "2026-08-06", "external_id": "order-b"},
                ]
            ),
            encoding="utf-8",
        )
        prepared = self.cli(
            "import-propose", "--input", str(source), "--format", "json",
            "--source-name", source.name, "--source-namespace", "fixture-shop",
            "--source-date", "2026-08-06",
        )
        operations = prepared["proposal"]["operations"]
        self.assertEqual([operation[0] for operation in operations], ["order", "order"])
        self.assertTrue(all(operation.count("--notes") == 1 for operation in operations))
        identities = [
            operation[operation.index("--import-unit-identity") + 1]
            for operation in operations
        ]
        self.assertEqual(len(set(identities)), 2)
        applied = self.cli("proposal-apply", prepared["proposal"]["proposal_id"])
        self.assertEqual(applied["checks"]["verification"]["failures"], [])
        items = [
            json.loads(line)
            for line in (self.root / "Data" / "store" / "items.jsonl").read_text().splitlines()
            if json.loads(line)["ownership_state"] == "candidate"
        ]
        self.assertEqual(len(items), 2)
        self.assertNotEqual(items[0]["item_id"], items[1]["item_id"])
        evidence = [
            json.loads(line)
            for line in (self.root / "Data" / "store" / "evidence.jsonl").read_text().splitlines()
            if json.loads(line)["source_ref"].startswith("import:fixture-shop:")
        ]
        self.assertEqual(len(evidence), 2)
        self.assertEqual(
            {json.loads(row["notes"])["generic_import"]["import"]["external_id"] for row in evidence},
            {"order-a", "order-b"},
        )

    def test_no_id_replay_cannot_become_distinct_and_import_provenance_stays_private(self) -> None:
        existing = self.cli(
            "order", "--actor", "privacy fixture", "--source-ref", "fixture", "--name", "Public model",
            "--category", "tool", "--ordered-on", "2026-08-06", "--order-placed", "--sensitivity", "low",
        )["result"]
        self.cli(
            "receive", "--actor", "privacy fixture", "--source-ref", "fixture", "--item-id", existing["item_id"],
            "--received-on", "2026-08-06", "--location-id", "loc-workshop",
        )
        private_source = self.scratch / "private-import.json"
        private_source.write_text(
            json.dumps([{
                "status": "ordered", "name": "Public model", "category": "tool", "date": "2026-08-06",
                "external_id": "CANARY-PRIVATE-ORDER", "notes": "CANARY-PRIVATE-RAW-ROW",
            }]),
            encoding="utf-8",
        )
        prepared = self.cli(
            "import-propose", "--input", str(private_source), "--format", "json",
            "--source-name", private_source.name, "--source-namespace", "private-shop",
            "--source-date", "2026-08-06", "--sensitivity", "personal",
        )
        self.cli("proposal-apply", prepared["proposal"]["proposal_id"])
        model = next(
            json.loads(line)
            for line in (self.root / "Data" / "store" / "models.jsonl").read_text().splitlines()
            if json.loads(line)["name"] == "Public model"
        )
        self.assertNotIn("CANARY", model["specs_json"])
        self.assertNotIn("CANARY", json.dumps(self.cli("show", existing["item_id"], scope="public")))
        self.assertNotIn("CANARY", json.dumps(self.cli("search", "Public model", scope="public")))
        self.assertNotIn("CANARY", self.catalogue.read_text(encoding="utf-8"))

        no_id = self.scratch / "no-id.json"
        no_id.write_text(
            json.dumps([{"status": "ordered", "name": "No ID model", "category": "tool", "date": "2026-08-06"}]),
            encoding="utf-8",
        )
        first = self.cli(
            "import-propose", "--input", str(no_id), "--format", "json", "--source-name", no_id.name,
            "--source-namespace", "fixture-shop", "--source-date", "2026-08-06",
        )
        self.cli("proposal-apply", first["proposal"]["proposal_id"])
        no_id.write_text(
            json.dumps([{"category": "tool", "date": "2026-08-07", "name": "No ID model", "status": "ordered"}]),
            encoding="utf-8",
        )
        replay = self.cli(
            "import-propose", "--input", str(no_id), "--format", "json", "--source-name", no_id.name,
            "--source-namespace", "fixture-shop", "--source-date", "2026-08-06",
        )
        self.assertIn(
            "matching unverified item already exists",
            self.fails("proposal-apply", replay["proposal"]["proposal_id"])["error"],
        )


if __name__ == "__main__":
    unittest.main()
