"""Real stdio acceptance tests for least-privilege MCP profiles."""

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
from PIL import Image

HERE = Path(__file__).resolve().parents[1]
CLI = HERE / "property_inventory.py"
MCP = Path(sys.executable).parent / "property-inventory-mcp"


class McpStdioTest(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory(prefix="inventory-mcp-")
        self.scratch = Path(self.temp.name)
        self.root = self.scratch / "inventory"
        self.runtime = self.scratch / "runtime"
        initialized = subprocess.run(
            [
                sys.executable,
                str(CLI),
                "--inventory-root",
                str(self.root),
                "--runtime-dir",
                str(self.runtime),
                "init",
            ],
            text=True,
            capture_output=True,
            check=False,
        )
        self.assertEqual(initialized.returncode, 0, initialized.stderr)
        self.assertTrue(MCP.is_file(), f"MCP console script not installed: {MCP}")

    def tearDown(self) -> None:
        self.temp.cleanup()

    def cli(self, *arguments: str) -> dict:
        completed = subprocess.run(
            [
                sys.executable,
                str(CLI),
                "--inventory-root",
                str(self.root),
                "--runtime-dir",
                str(self.runtime),
                *arguments,
            ],
            text=True,
            capture_output=True,
            check=False,
        )
        self.assertEqual(completed.returncode, 0, completed.stderr or completed.stdout)
        return json.loads(completed.stdout)

    def parameters(
        self,
        *,
        profile: str,
        scope: str,
        forbidden_roots: tuple[Path, ...] = (),
    ) -> StdioServerParameters:
        arguments = [
            "--inventory-root",
            str(self.root),
            "--runtime-dir",
            str(self.runtime),
            "--scope",
            scope,
            "--profile",
            profile,
        ]
        for forbidden_root in forbidden_roots:
            arguments.extend(("--forbidden-root", str(forbidden_root)))
        return StdioServerParameters(
            command=str(MCP),
            args=arguments,
            cwd=self.scratch,
        )

    def test_forbidden_roots_are_rechecked_by_each_tool_invocation(self) -> None:
        forbidden = self.scratch / "forbidden"
        forbidden.mkdir()

        async def scenario() -> None:
            async with Client(
                stdio_client(
                    self.parameters(
                        profile="read",
                        scope="personal",
                        forbidden_roots=(forbidden,),
                    )
                ),
                mode="legacy",
            ) as client:
                healthy = await client.call_tool("inventory_status", {})
                self.assertFalse(healthy.is_error)

                moved = forbidden / "inventory"
                self.root.rename(moved)
                self.root.symlink_to(moved, target_is_directory=True)

                rejected = await client.call_tool("inventory_status", {})
                self.assertTrue(rejected.is_error)
                self.assertIn("could not complete safely", str(rejected.content).lower())

        asyncio.run(scenario())

    def test_write_profile_validates_runtime_before_persisting_proposal_input(self) -> None:
        forbidden = self.scratch / "forbidden-write"
        forbidden.mkdir()

        def snapshot(path: Path) -> dict[str, bytes]:
            return {
                child.relative_to(path).as_posix(): child.read_bytes()
                for child in path.rglob("*")
                if child.is_file()
            }

        async def scenario() -> None:
            async with Client(
                stdio_client(
                    self.parameters(
                        profile="write",
                        scope="private",
                        forbidden_roots=(forbidden,),
                    )
                ),
                mode="legacy",
            ) as client:
                moved = forbidden / "runtime"
                self.runtime.rename(moved)
                before = snapshot(moved)
                self.runtime.symlink_to(moved, target_is_directory=True)

                rejected = await client.call_tool(
                    "prepare_inventory_proposal",
                    {
                        "operations": [
                            [
                                "add-location",
                                "--name",
                                "must never persist",
                                "--kind",
                                "room",
                            ],
                        ]
                    },
                )
                self.assertTrue(rejected.is_error)
                self.assertIn("forbidden root", str(rejected.content).lower())
                self.assertEqual(snapshot(moved), before)

                self.runtime.unlink()
                moved.rename(self.runtime)
                healthy = await client.call_tool("inventory_status", {})
                self.assertFalse(healthy.is_error)

        asyncio.run(scenario())

    def test_write_profile_never_reads_file_backed_generic_proposal_operations(self) -> None:
        forbidden = self.scratch / "forbidden-input"
        forbidden.mkdir()
        secret = forbidden / "private-floorplan.json"
        secret.write_text(
            json.dumps(
                {
                    "type": "FeatureCollection",
                    "features": [],
                    "private_canary": "MCP_MUST_NOT_RETURN_THIS_FILE",
                }
            ),
            encoding="utf-8",
        )
        proposal_directory = self.runtime / "proposals"
        before = sorted(path.name for path in proposal_directory.glob("*.json"))

        async def scenario() -> None:
            async with Client(
                stdio_client(
                    self.parameters(
                        profile="write",
                        scope="private",
                        forbidden_roots=(forbidden,),
                    )
                ),
                mode="legacy",
            ) as client:
                rejected = await client.call_tool(
                    "prepare_inventory_proposal",
                    {
                        "operations": [
                            [
                                "import-floorplan",
                                "--input",
                                str(secret),
                                "--source-ref",
                                "must not be read",
                                "--captured-on",
                                "2026-08-06",
                            ]
                        ]
                    },
                )
                self.assertTrue(rejected.is_error)
                response = str(rejected.content)
                self.assertIn("not available through mcp", response.casefold())
                self.assertNotIn("MCP_MUST_NOT_RETURN_THIS_FILE", response)

        asyncio.run(scenario())
        self.assertEqual(
            sorted(path.name for path in proposal_directory.glob("*.json")), before
        )

    def test_default_read_profile_has_no_mutation_surface(self) -> None:
        async def scenario() -> None:
            async with Client(
                stdio_client(self.parameters(profile="read", scope="personal")),
                mode="legacy",
            ) as client:
                listed = await client.list_tools()
                names = {tool.name for tool in listed.tools}
                self.assertEqual(
                    names,
                    {
                        "inventory_status",
                        "get_insurance_readiness",
                        "get_upkeep_report",
                        "search_inventory",
                        "inventory_task_context",
                        "list_inventory_items",
                        "list_inventory_locations",
                        "get_inventory_item",
                        "get_kit_status",
                        "check_torque_path",
                        "check_compatibility",
                        "get_space_context",
                        "check_spatial_fit",
                        "calculate_free_volume",
                        "plan_spatial_packing",
                    },
                )
                self.assertFalse(
                    names
                    & {
                        "prepare_inventory_proposal",
                        "apply_inventory_proposal",
                        "order",
                        "move",
                        "sell",
                        "receive",
                        "physical_check",
                    }
                )
                status = await client.call_tool("inventory_status", {})
                self.assertEqual(status.structured_content["status"], "pass")
                self.assertEqual(status.structured_content["scope"], "personal")
                search = await client.call_tool(
                    "search_inventory", {"query": "not-recorded-fixture"}
                )
                self.assertFalse(search.structured_content["recorded"])
                self.assertEqual(
                    search.structured_content["meaning_if_empty"], "unknown, not absent"
                )
                option_like = await client.call_tool(
                    "search_inventory", {"query": "--help"}
                )
                self.assertFalse(option_like.structured_content["recorded"])
                still_alive = await client.call_tool("inventory_status", {})
                self.assertEqual(still_alive.structured_content["status"], "pass")

        asyncio.run(scenario())

    def test_read_mcp_compatibility_is_structured_and_does_not_mutate(self) -> None:
        first = self.cli(
            "order",
            "--actor",
            "MCP compatibility fixture",
            "--source-ref",
            "MCP compatibility fixture source",
            "--name",
            "MCP compatibility first",
            "--category",
            "test fixture",
            "--ordered-on",
            "2026-08-06",
            "--order-placed",
        )["result"]["item_id"]
        second = self.cli(
            "order",
            "--actor",
            "MCP compatibility fixture",
            "--source-ref",
            "MCP compatibility fixture source",
            "--name",
            "MCP compatibility second",
            "--category",
            "test fixture",
            "--ordered-on",
            "2026-08-06",
            "--order-placed",
        )["result"]["item_id"]
        before = {
            path.relative_to(self.root).as_posix(): path.read_bytes()
            for path in (self.root / "Data" / "store").glob("*.jsonl")
        }

        async def scenario() -> None:
            async with Client(
                stdio_client(self.parameters(profile="read", scope="personal")),
                mode="legacy",
            ) as client:
                result = await client.call_tool(
                    "check_compatibility",
                    {"first_item_id": first, "second_item_id": second},
                )
                self.assertFalse(result.is_error, result.content)
                self.assertEqual(
                    result.structured_content,
                    {
                        "availability": {
                            "first": {
                                "available": False,
                                "condition": None,
                                "condition_state": "unknown",
                                "ownership_state": "candidate",
                                "possession_available": False,
                                "reason": "item is not operationally available in ownership state candidate",
                            },
                            "second": {
                                "available": False,
                                "condition": None,
                                "condition_state": "unknown",
                                "ownership_state": "candidate",
                                "possession_available": False,
                                "reason": "item is not operationally available in ownership state candidate",
                            },
                        },
                        "first_item_id": first,
                        "second_item_id": second,
                        "outcome": "unknown",
                        "operational_outcome": "unknown",
                        "reason": (
                            "no sufficient normalized compatibility evidence; legacy text "
                            "or similar purpose does not prove interchangeability"
                        ),
                        "evidence_ids": [],
                    },
                )

        asyncio.run(scenario())
        after = {
            path.relative_to(self.root).as_posix(): path.read_bytes()
            for path in (self.root / "Data" / "store").glob("*.jsonl")
        }
        self.assertEqual(after, before)

    def test_read_mcp_calls_paginate_and_cover_auditable_operational_context(self) -> None:
        for location_id, name, kind in (
            ("loc-mcp-alpha", "MCP alpha room", "room"),
            ("loc-mcp-beta", "MCP beta room", "room"),
            ("loc-mcp-box", "MCP measured box", "container"),
        ):
            self.cli(
                "add-location",
                "--location-id", location_id,
                "--name", name,
                "--kind", kind,
                "--sensitivity", "low",
            )
        served = self.cli(
            "order",
            "--actor", "MCP read fixture",
            "--source-ref", "MCP served-item purchase record",
            "--name", "MCP served tool",
            "--category", "tool",
            "--ordered-on", "2026-08-06",
            "--order-placed",
            "--sensitivity", "low",
        )["result"]
        served_check = self.cli(
            "physical-check",
            "--actor", "MCP read fixture",
            "--item-id", served["item_id"],
            "--checked-on", "2026-08-06",
            "--location-id", "loc-mcp-alpha",
            "--source-ref", "MCP served-tool physical check",
            "--condition", "functional",
        )["result"]
        component = self.cli(
            "order",
            "--actor", "MCP read fixture",
            "--source-ref", "MCP component purchase record",
            "--name", "MCP component tool",
            "--category", "tool",
            "--ordered-on", "2026-08-06",
            "--order-placed",
            "--sensitivity", "low",
        )["result"]
        component_check = self.cli(
            "physical-check",
            "--actor", "MCP read fixture",
            "--item-id", component["item_id"],
            "--checked-on", "2026-08-06",
            "--location-id", "loc-mcp-beta",
            "--source-ref", "MCP component physical check",
            "--condition", "working",
        )["result"]
        kit = self.cli(
            "add-kit",
            "--kit-id", "kit-mcp-read",
            "--name", "MCP operational kit",
            "--serves-item-id", served["item_id"],
            "--evidence-id", served_check["evidence_id"],
        )["result"]
        self.cli(
            "set-kit-requirement",
            "--actor", "MCP read fixture",
            "--kit-id", kit["kit_id"],
            "--requirement-key", "component",
            "--item-id", component["item_id"],
            "--status", "source_present",
            "--evidence-id", component_check["evidence_id"],
        )
        self.cli(
            "review-kit",
            "--actor", "MCP read fixture",
            "--kit-id", kit["kit_id"],
            "--reviewed-on", "2026-08-06",
            "--completeness", "complete",
            "--source-ref", "MCP kit review",
        )
        self.cli(
            "add-torque-path",
            "--path-id", "torque-mcp-read",
            "--tool-item-id", served["item_id"],
            "--output-drive", "1/4 inch",
            "--min-torque-nm", "2",
            "--max-torque-nm", "10",
            "--status", "direct",
            "--evidence-id", served_check["evidence_id"],
        )
        self.cli(
            "add-space",
            "--location-id", "loc-mcp-box",
            "--source-ref", "MCP measured box fixture",
            "--captured-on", "2026-08-06",
            "--sensitivity", "low",
            "--profile",
            '{"kind":"container_box","x":0,"y":0,"z":0,"width":10,"height":5,"depth":5,"unit":"cm"}',
        )

        async def scenario() -> None:
            async with Client(
                stdio_client(self.parameters(profile="read", scope="private")),
                mode="legacy",
            ) as client:
                locations_first = await client.call_tool(
                    "list_inventory_locations", {"limit": 1}
                )
                self.assertFalse(locations_first.is_error, locations_first.content)
                locations_second = await client.call_tool(
                    "list_inventory_locations",
                    {"limit": 1, "cursor": locations_first.structured_content["next_cursor"]},
                )
                self.assertFalse(locations_second.is_error, locations_second.content)
                self.assertNotEqual(
                    locations_first.structured_content["matches"][0]["location_id"],
                    locations_second.structured_content["matches"][0]["location_id"],
                )

                items_first = await client.call_tool(
                    "list_inventory_items", {"limit": 1, "category": "tool"}
                )
                self.assertFalse(items_first.is_error, items_first.content)
                items_second = await client.call_tool(
                    "list_inventory_items",
                    {
                        "limit": 1,
                        "category": "tool",
                        "cursor": items_first.structured_content["next_cursor"],
                    },
                )
                self.assertFalse(items_second.is_error, items_second.content)
                self.assertNotEqual(
                    items_first.structured_content["matches"][0]["item"]["item_id"],
                    items_second.structured_content["matches"][0]["item"]["item_id"],
                )

                context = await client.call_tool(
                    "get_inventory_item", {"item_id": served["item_id"]}
                )
                self.assertFalse(context.is_error, context.content)
                self.assertIn(served_check["evidence_id"], context.structured_content["evidence_ids"])
                self.assertIn(
                    "physically_verified",
                    {event["event_type"] for event in context.structured_content["events"]},
                )

                kit_status = await client.call_tool(
                    "get_kit_status", {"kit_id": kit["kit_id"]}
                )
                self.assertFalse(kit_status.is_error, kit_status.content)
                self.assertEqual(
                    kit_status.structured_content["kits"][0]["readiness"], "verified_ready"
                )
                torque = await client.call_tool(
                    "check_torque_path", {"path_id": "torque-mcp-read", "requested_nm": 6}
                )
                self.assertFalse(torque.is_error, torque.content)
                self.assertEqual(torque.structured_content["decisions"][0]["outcome"], "safe")
                free_volume = await client.call_tool(
                    "calculate_free_volume",
                    {"location_id": "loc-mcp-box", "occupied_boxes": []},
                )
                self.assertFalse(free_volume.is_error, free_volume.content)
                self.assertEqual(free_volume.structured_content["status"], "known")
                self.assertEqual(free_volume.structured_content["free_volume"], 250.0)

        asyncio.run(scenario())

    def test_private_mcp_capture_decision_prepares_without_applying(self) -> None:
        self.cli(
            "add-location",
            "--location-id", "loc-mcp-capture",
            "--name", "MCP capture room",
            "--kind", "room",
            "--sensitivity", "low",
        )
        overview = self.scratch / "mcp-capture.png"
        Image.new("RGB", (12, 9), (20, 30, 40)).save(overview)
        before = {
            path.name: path.read_bytes()
            for path in (self.root / "Data" / "store").glob("*.jsonl")
        }

        async def scenario() -> None:
            async with Client(
                stdio_client(self.parameters(profile="write", scope="private")),
                mode="legacy",
            ) as client:
                prepared = await client.call_tool(
                    "prepare_overview_capture",
                    {
                        "overview_path": str(overview),
                        "captured_on": "2026-08-06",
                        "segments": [
                            {
                                "segment_id": "fixture-label",
                                "region": {"x": 2, "y": 1, "width": 5, "height": 4},
                            }
                        ],
                        "source_ref": "MCP capture decision fixture",
                    },
                )
                self.assertFalse(prepared.is_error, prepared.content)
                capture = prepared.structured_content["capture"]
                artifact = json.loads(
                    (
                        self.runtime
                        / "capture-staging"
                        / capture["capture_session_id"]
                        / "artifact.json"
                    ).read_text(encoding="utf-8")
                )
                decision = {
                    "crop_id": artifact["crops"][0]["crop_id"],
                    "segment_id": "fixture-label",
                    "observation_id": None,
                    "item_id": None,
                    "discovery": {
                        "name": "MCP capture discovered tool",
                        "category": "tool",
                        "new_model": True,
                        "new_unit": True,
                        "sensitivity": "low",
                        "specs": {},
                        "identifiers": {},
                    },
                    "physical": {
                        "actor": "MCP capture reviewer",
                        "checked_on": "2026-08-06",
                        "condition": "good",
                        "container_id": None,
                        "location_id": "loc-mcp-capture",
                        "notes": None,
                        "quantity": 1,
                        "serial_or_lot": None,
                        "unit": "item",
                    },
                }
                reviewed = await client.call_tool(
                    "review_overview_capture",
                    {
                        "capture_session_id": capture["capture_session_id"],
                        "artifact_sha256": capture["artifact_sha256"],
                        "links": {},
                        "decisions": [decision],
                    },
                )
                self.assertFalse(reviewed.is_error, reviewed.content)
                self.assertEqual(reviewed.structured_content["status"], "prepared")
                self.assertEqual(
                    reviewed.structured_content["proposal"]["status"], "prepared"
                )

        asyncio.run(scenario())
        after = {
            path.name: path.read_bytes()
            for path in (self.root / "Data" / "store").glob("*.jsonl")
        }
        self.assertEqual(after, before)

    def test_public_and_personal_mcp_reads_remove_private_canaries(self) -> None:
        self.cli(
            "add-location",
            "--location-id", "loc-mcp-scope",
            "--name", "MCP scope fixture room",
            "--kind", "room",
            "--sensitivity", "low",
        )
        item_id = self.cli(
            "order",
            "--actor",
            "CANARY_PRIVATE_ACTOR",
            "--source-ref",
            "CANARY_PRIVATE_ORDER_SOURCE",
            "--notes",
            "CANARY_PRIVATE_ITEM_NOTE",
            "--name",
            "mcp-scope-canary-object",
            "--category",
            "test fixture",
            "--ordered-on",
            "2026-08-05",
            "--order-placed",
            "--identifiers",
            '{"private":"CANARY_PRIVATE_IDENTIFIER"}',
            "--reference-url",
            "https://private.example/CANARY_PRIVATE_REFERENCE",
            "--receipt-ref",
            "CANARY_PRIVATE_RECEIPT",
            "--sensitivity",
            "low",
        )["result"]["item_id"]
        self.cli(
            "receive",
            "--actor",
            "CANARY_PRIVATE_RECEIVE_ACTOR",
            "--source-ref",
            "CANARY_PRIVATE_PHYSICAL_SOURCE",
            "--notes",
            "CANARY_PRIVATE_EVENT_NOTE",
            "--item-id",
            item_id,
            "--received-on",
            "2026-08-06",
            "--location-id",
            "loc-mcp-scope",
            "--serial-or-lot",
            "CANARY_PRIVATE_SERIAL",
            "--physical-check",
        )
        companion_id = self.cli(
            "order",
            "--actor",
            "Scope fixture",
            "--source-ref",
            "Companion source",
            "--name",
            "mcp-scope-companion",
            "--category",
            "test fixture",
            "--ordered-on",
            "2026-08-05",
            "--order-placed",
            "--sensitivity",
            "low",
        )["result"]["item_id"]
        self.cli(
            "relate",
            "--subject-item-id",
            item_id,
            "--object-item-id",
            companion_id,
            "--predicate",
            "unknown",
            "--confidence",
            "unknown",
            "--captured-on",
            "2026-08-06",
            "--evidence-type",
            "research",
            "--claim-strength",
            "research_only",
            "--source-ref",
            "CANARY_PRIVATE_RELATIONSHIP_SOURCE",
            "--notes",
            "CANARY_PRIVATE_RELATIONSHIP_NOTE",
        )
        upkeep = self.cli(
            "maintenance-start",
            "--performed-on",
            "2026-08-06",
            "--activity",
            "CANARY_PRIVATE_UPKEEP_ACTIVITY",
            "--source-ref",
            "CANARY_PRIVATE_UPKEEP_SOURCE",
            "--sensitivity",
            "high",
        )
        self.cli(
            "maintenance-finish",
            upkeep["maintenance_session_id"],
            "--elapsed-seconds",
            "5",
            "--correction-count",
            "0",
            "--review-count",
            "0",
        )
        private_canaries = (
            "CANARY_PRIVATE_ACTOR",
            "CANARY_PRIVATE_ORDER_SOURCE",
            "CANARY_PRIVATE_ITEM_NOTE",
            "CANARY_PRIVATE_IDENTIFIER",
            "CANARY_PRIVATE_REFERENCE",
            "CANARY_PRIVATE_RECEIPT",
            "CANARY_PRIVATE_RECEIVE_ACTOR",
            "CANARY_PRIVATE_PHYSICAL_SOURCE",
            "CANARY_PRIVATE_EVENT_NOTE",
            "CANARY_PRIVATE_SERIAL",
            "CANARY_PRIVATE_RELATIONSHIP_SOURCE",
            "CANARY_PRIVATE_RELATIONSHIP_NOTE",
            "CANARY_PRIVATE_UPKEEP_ACTIVITY",
            "CANARY_PRIVATE_UPKEEP_SOURCE",
        )

        async def scenario(scope: str) -> None:
            async with Client(
                stdio_client(self.parameters(profile="read", scope=scope)),
                mode="legacy",
            ) as client:
                search = await client.call_tool(
                    "search_inventory", {"query": "mcp-scope-canary-object"}
                )
                shown = await client.call_tool(
                    "get_inventory_item", {"item_id": item_id}
                )
                upkeep_report = await client.call_tool("get_upkeep_report", {})
                self.assertFalse(search.is_error)
                self.assertFalse(shown.is_error)
                self.assertFalse(upkeep_report.is_error)
                serialized = json.dumps(
                    [
                        search.structured_content,
                        shown.structured_content,
                        upkeep_report.structured_content,
                    ],
                    sort_keys=True,
                )
                for canary in private_canaries:
                    self.assertNotIn(canary, serialized)
                item = shown.structured_content
                self.assertEqual(item["model"]["identifiers"], {})
                self.assertIsNone(item["model"]["reference_url"])
                self.assertIsNone(item["item"]["receipt_ref"])
                self.assertIsNone(item["item"]["serial_or_lot"])
                self.assertIsNone(item["item"]["notes"])
                self.assertEqual(item["evidence_ids"], [])
                self.assertTrue(
                    all(
                        relationship["evidence_id"] is None
                        and relationship["notes"] is None
                        for relationship in item["relationships"]
                    )
                )

        for scope in ("public", "personal"):
            with self.subTest(scope=scope):
                asyncio.run(scenario(scope))

    def test_private_write_profile_prepares_and_inspects_but_never_applies(self) -> None:
        async def scenario() -> str:
            async with Client(
                stdio_client(self.parameters(profile="write", scope="private")),
                mode="legacy",
            ) as client:
                listed = await client.list_tools()
                names = {tool.name for tool in listed.tools}
                self.assertEqual(
                    names,
                    {
                        "inventory_status",
                        "get_insurance_readiness",
                        "get_upkeep_report",
                        "search_inventory",
                        "inventory_task_context",
                        "list_inventory_items",
                        "list_inventory_locations",
                        "get_inventory_item",
                        "get_kit_status",
                        "check_torque_path",
                        "check_compatibility",
                        "get_space_context",
                        "check_spatial_fit",
                        "calculate_free_volume",
                        "plan_spatial_packing",
                        "show_inventory_proposal",
                        "capture_status",
                        "prepare_inventory_proposal",
                        "prepare_physical_discovery",
                        "prepare_overview_capture",
                        "review_overview_capture",
                        "prepare_replica_sync",
                        "inspect_replica_sync",
                        "resolve_replica_sync",
                    },
                )
                prepared = await client.call_tool(
                    "prepare_inventory_proposal",
                    {
                        "operations": [
                            [
                                "add-location",
                                "--name",
                                "MCP fixture room",
                                "--location-id",
                                "loc-mcp-fixture",
                                "--kind",
                                "room",
                            ],
                            [
                                "ownership-start",
                                "--actor",
                                "MCP fixture",
                                "--source-ref",
                                "review-only owner observation",
                                "--item-id",
                                "itm-review-only",
                                "--party-id",
                                "party-review-only",
                                "--started-on",
                                "2026-08-10",
                            ],
                            [
                                "custody-start",
                                "--actor",
                                "MCP fixture",
                                "--source-ref",
                                "review-only custody observation",
                                "--item-id",
                                "itm-review-only",
                                "--custody-kind",
                                "unknown",
                                "--started-on",
                                "2026-08-10",
                            ],
                        ]
                    },
                )
                proposal_id = prepared.structured_content["proposal"]["proposal_id"]
                before = (self.root / "Data" / "store" / "locations.jsonl").read_text()
                self.assertNotIn("loc-mcp-fixture", before)

                shown = await client.call_tool(
                    "show_inventory_proposal", {"proposal_id": proposal_id}
                )
                self.assertEqual(shown.structured_content["proposal"]["status"], "prepared")
                self.assertEqual(
                    [
                        operation[0]
                        for operation in shown.structured_content["proposal"]["operations"]
                    ],
                    ["add-location", "ownership-start", "custody-start"],
                )
                self.assertNotIn("apply_inventory_proposal", names)
                self.assertNotIn("apply_replica_sync", names)
                self.assertEqual(
                    (self.root / "Data" / "store" / "locations.jsonl").read_text(), before
                )
                return proposal_id

        proposal_id = asyncio.run(scenario())
        locations = (self.root / "Data" / "store" / "locations.jsonl").read_text()
        self.assertNotIn("loc-mcp-fixture", locations)
        proposal = self.cli("proposal-show", proposal_id)
        self.assertEqual(proposal["proposal"]["status"], "prepared")


if __name__ == "__main__":
    unittest.main()
