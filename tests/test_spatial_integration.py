"""CLI and stdio MCP integration tests for evidence-safe spatial profiles."""

from __future__ import annotations

import asyncio
import json
import os
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
FLOORPLAN_TEMPLATE = HERE / "tests" / "fixtures" / "spatial" / "synthetic-zones.geojson"


class SpatialIntegrationTest(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory(prefix="inventory-spatial-integration-")
        self.scratch = Path(self.temp.name)
        self.root = self.scratch / "inventory"
        self.runtime = self.scratch / "runtime"
        self.cli("init")
        self.add_location("loc-fixture-public-zone", "Fixture public zone", "low")
        self.add_location("loc-fixture-restricted-zone", "Fixture restricted zone", "high")
        self.add_location("loc-fixture-reserved-zone", "Fixture reserved zone", "high")
        self.unrelated_evidence = self.order("floor-plan purchase", "record", "low")
        self.floorplan = self.scratch / "synthetic-zones.geojson"
        template = FLOORPLAN_TEMPLATE.read_text(encoding="utf-8")
        self.floorplan.write_text(
            template.replace("{{PUBLIC_ZONE_LOCATION_ID}}", "loc-fixture-public-zone")
            .replace("{{RESTRICTED_ZONE_LOCATION_ID}}", "loc-fixture-restricted-zone")
            .replace("{{RESERVED_ZONE_LOCATION_ID}}", "loc-fixture-reserved-zone"),
            encoding="utf-8",
        )

    def tearDown(self) -> None:
        self.temp.cleanup()

    @property
    def store(self) -> Path:
        return self.root / "Data" / "store"

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

    def add_location(self, location_id: str, name: str, sensitivity: str) -> None:
        self.cli(
            "add-location",
            "--location-id",
            location_id,
            "--name",
            name,
            "--kind",
            "room",
            "--sensitivity",
            sensitivity,
        )

    def order(self, name: str, category: str, sensitivity: str) -> dict:
        return self.cli(
            "order",
            "--actor",
            "Spatial integration test",
            "--source-ref",
            f"Checked evidence for {name}",
            "--name",
            name,
            "--category",
            category,
            "--ordered-on",
            "2026-08-06",
            "--order-placed",
            "--sensitivity",
            sensitivity,
        )["result"]

    def parameters(self, scope: str) -> StdioServerParameters:
        self.assertTrue(MCP.is_file(), f"MCP console script not installed: {MCP}")
        return StdioServerParameters(
            command=str(MCP),
            args=[
                "--inventory-root",
                str(self.root),
                "--runtime-dir",
                str(self.runtime),
                "--scope",
                scope,
                "--profile",
                "read",
            ],
            cwd=self.scratch,
        )

    def import_floorplan(self) -> dict:
        return self.cli(
            "import-floorplan",
            "--input",
            str(self.floorplan),
            "--source-ref",
            "Synthetic fixture zone dimensions",
            "--captured-on",
            "2026-08-05",
            "--sensitivity",
            "low",
        )["result"]

    def add_visible_box(self) -> None:
        self.cli(
            "add-space",
            "--location-id",
            "loc-fixture-public-zone",
            "--source-ref",
            "Synthetic fixture container",
            "--captured-on",
            "2026-08-06",
            "--sensitivity",
            "low",
            "--profile",
            json.dumps(
                {
                    "kind": "container_box",
                    "x": 10,
                    "y": 20,
                    "z": 30,
                    "width": 4,
                    "height": 1,
                    "depth": 1,
                    "unit": "cm",
                }
            ),
        )

    def test_structured_input_paths_reject_links_special_files_size_and_duplicate_keys(
        self,
    ) -> None:
        before = {path.name: path.read_bytes() for path in self.store.glob("*.jsonl")}
        linked = self.scratch / "linked-floorplan.geojson"
        linked.symlink_to(self.floorplan)
        linked_error = self.cli_fails(
            "import-floorplan",
            "--input",
            str(linked),
            "--source-ref",
            "Linked input must fail",
            "--captured-on",
            "2026-08-06",
            "--sensitivity",
            "low",
        )
        self.assertIn("regular file", linked_error["error"])

        fifo = self.scratch / "locations.fifo"
        os.mkfifo(fifo)
        blocked = subprocess.run(
            self.command("import-locations", "--input", str(fifo)),
            text=True,
            capture_output=True,
            check=False,
            timeout=2,
        )
        self.assertNotEqual(blocked.returncode, 0)
        self.assertIn("regular file", json.loads(blocked.stderr)["error"])

        duplicate = self.scratch / "duplicate-locations.json"
        duplicate.write_text(
            '[{"location_id":"loc-x","location_id":"loc-y",'
            '"name":"X","kind":"room","sensitivity":"low"}]',
            encoding="utf-8",
        )
        duplicate_error = self.cli_fails("import-locations", "--input", str(duplicate))
        self.assertIn("duplicate JSON key", duplicate_error["error"])

        oversized = self.scratch / "oversized-operations.json"
        with oversized.open("wb") as handle:
            handle.truncate(16 * 1024 * 1024 + 1)
        oversized_error = self.cli_fails("propose", "--operations", str(oversized))
        self.assertIn("too large", oversized_error["error"])
        self.assertEqual(
            {path.name: path.read_bytes() for path in self.store.glob("*.jsonl")},
            before,
        )

    def test_geojson_is_idempotent_scope_safe_and_never_moves_an_item(self) -> None:
        before_items = self.store.joinpath("items.jsonl").read_bytes()
        before_events = self.store.joinpath("inventory_events.jsonl").read_bytes()
        first = self.import_floorplan()
        self.assertEqual(len(first["profiles"]), 3)
        self.assertTrue(all(not result["reused"] for result in first["profiles"]))
        self.assertEqual(self.store.joinpath("items.jsonl").read_bytes(), before_items)
        self.assertEqual(self.store.joinpath("inventory_events.jsonl").read_bytes(), before_events)
        visible = self.cli("space", "loc-fixture-public-zone", scope="public")
        self.assertEqual(visible["status"], "known")
        self.assertEqual(visible["profiles"][0]["kind"], "floor_rectangle")
        self.assertEqual(visible["profiles"][0]["width"], 4.0)
        self.assertEqual(visible["profiles"][0]["height"], 3.0)
        self.assertTrue(visible["profiles"][0]["profile_id"].startswith("spatial-"))
        self.assertEqual(visible["profiles"][0]["evidence"]["evidence_type"], "research")
        hidden = self.cli("space", "loc-fixture-restricted-zone", scope="public")
        self.assertEqual(
            hidden, {"status": "unknown", "reason": "space_not_visible_or_not_recorded"}
        )
        self.assertNotIn("private", json.dumps(hidden).casefold())
        second = self.import_floorplan()
        self.assertTrue(all(result["reused"] for result in second["profiles"]))
        unchanged = self.store.joinpath("spatial_profiles.jsonl").read_bytes()
        changed = json.loads(self.floorplan.read_text(encoding="utf-8"))
        changed["features"][0]["geometry"]["coordinates"][0][1] = [3.5, 0]
        changed["features"][0]["geometry"]["coordinates"][0][2] = [3.5, 3]
        self.floorplan.write_text(json.dumps(changed), encoding="utf-8")
        rejected = self.cli_fails(
            "import-floorplan",
            "--input",
            str(self.floorplan),
            "--source-ref",
            "Synthetic fixture zone dimensions",
            "--captured-on",
            "2026-08-05",
            "--sensitivity",
            "low",
        )
        self.assertIn("different checked content", rejected["error"])
        self.assertEqual(self.store.joinpath("spatial_profiles.jsonl").read_bytes(), unchanged)

    def test_malformed_geojson_and_failed_proposal_are_atomic(self) -> None:
        malformed = self.scratch / "malformed.geojson"
        malformed.write_text(
            json.dumps(
                {
                    "type": "FeatureCollection",
                    "features": [
                        {
                            "type": "Feature",
                            "id": "not-a-rectangle",
                            "properties": {
                                "location_id": "loc-fixture-public-zone",
                                "evidence_id": self.unrelated_evidence["evidence_id"],
                                "sensitivity": "low",
                                "unit": "m",
                            },
                            "geometry": {
                                "type": "Polygon",
                                "coordinates": [[[0, 0], [1, 1], [1, 0], [0, 0], [0, 0]]],
                            },
                        }
                    ],
                }
            ),
            encoding="utf-8",
        )
        before = self.store.joinpath("spatial_profiles.jsonl").read_bytes()
        self.cli_fails("import-floorplan", "--input", str(malformed))
        self.assertEqual(self.store.joinpath("spatial_profiles.jsonl").read_bytes(), before)
        operations = self.scratch / "operations.json"
        operations.write_text(
            json.dumps(
                [
                    [
                        "import-floorplan",
                        "--input",
                        str(self.floorplan),
                        "--source-ref",
                        "Synthetic fixture zone dimensions",
                        "--captured-on",
                        "2026-08-05",
                        "--sensitivity",
                        "low",
                    ],
                    [
                        "add-space",
                        "--location-id",
                        "loc-fixture-public-zone",
                        "--evidence-id",
                        "ev-does-not-exist",
                        "--sensitivity",
                        "low",
                        "--profile",
                        '{"kind":"container_box","x":0,"y":0,"z":0,"width":1,"height":1,"depth":1,"unit":"cm"}',
                    ],
                ]
            ),
            encoding="utf-8",
        )
        proposal = self.cli("propose", "--operations", str(operations))["proposal"]
        self.cli_fails("proposal-apply", proposal["proposal_id"])
        self.assertEqual(self.store.joinpath("spatial_profiles.jsonl").read_bytes(), before)

    def test_source_created_evidence_needs_no_item_or_existing_evidence_row(self) -> None:
        document = json.loads(self.floorplan.read_text(encoding="utf-8"))
        document["features"] = [document["features"][0]]
        source_floorplan = self.scratch / "checked-fixture-public-zone.geojson"
        source_floorplan.write_text(json.dumps(document), encoding="utf-8")
        item_evidence_before = self.store.joinpath("item_evidence.jsonl").read_bytes()
        events_before = self.store.joinpath("inventory_events.jsonl").read_bytes()
        first = self.cli(
            "import-floorplan",
            "--input",
            str(source_floorplan),
            "--source-ref",
            "Synthetic fixture public-zone floor plan",
            "--captured-on",
            "2026-08-06",
            "--sensitivity",
            "low",
        )["result"]
        self.assertTrue(first["evidence_created"])
        self.assertTrue(first["evidence_id"].startswith("ev-space-"))
        self.assertEqual(
            self.store.joinpath("item_evidence.jsonl").read_bytes(), item_evidence_before
        )
        self.assertEqual(self.store.joinpath("inventory_events.jsonl").read_bytes(), events_before)
        second = self.cli(
            "import-floorplan",
            "--input",
            str(source_floorplan),
            "--source-ref",
            "Synthetic fixture public-zone floor plan",
            "--captured-on",
            "2026-08-06",
            "--sensitivity",
            "low",
        )["result"]
        self.assertFalse(second["evidence_created"])
        self.assertEqual(second["evidence_id"], first["evidence_id"])

    def test_proposal_binds_floorplan_bytes_and_purchase_evidence_cannot_certify_space(
        self,
    ) -> None:
        operations = self.scratch / "bound-operations.json"
        operations.write_text(
            json.dumps(
                [
                    [
                        "import-floorplan",
                        "--input",
                        str(self.floorplan),
                        "--source-ref",
                        "Synthetic fixture zone dimensions",
                        "--captured-on",
                        "2026-08-05",
                        "--sensitivity",
                        "low",
                    ]
                ]
            ),
            encoding="utf-8",
        )
        proposal = self.cli("propose", "--operations", str(operations))["proposal"]
        self.assertIn("--document-json", proposal["operations"][0])
        self.assertNotIn(str(self.floorplan), proposal["operations"][0])
        changed = json.loads(self.floorplan.read_text(encoding="utf-8"))
        changed["features"][0]["geometry"]["coordinates"][0][1] = [9, 0]
        changed["features"][0]["geometry"]["coordinates"][0][2] = [9, 3]
        self.floorplan.write_text(json.dumps(changed), encoding="utf-8")
        self.cli("proposal-apply", proposal["proposal_id"])
        profile = self.cli("space", "loc-fixture-public-zone")["profiles"][0]
        self.assertEqual(profile["width"], 4.0)

        unrelated = json.loads(FLOORPLAN_TEMPLATE.read_text(encoding="utf-8"))
        unrelated["features"] = [unrelated["features"][0]]
        properties = unrelated["features"][0]["properties"]
        properties["location_id"] = "loc-fixture-public-zone"
        properties["evidence_id"] = self.unrelated_evidence["evidence_id"]
        unrelated_path = self.scratch / "unrelated-evidence.geojson"
        unrelated_path.write_text(json.dumps(unrelated), encoding="utf-8")
        rejected = self.cli_fails("import-floorplan", "--input", str(unrelated_path))
        self.assertIn("spatial profiles require", rejected["error"])

    def test_proposal_rejects_forbidden_floorplan_inputs_and_strips_unused_fields(self) -> None:
        forbidden = self.scratch / "forbidden-floorplans"
        forbidden.mkdir()
        document = json.loads(self.floorplan.read_text(encoding="utf-8"))
        document["private_canary"] = "TOP_LEVEL_MUST_NOT_BE_FROZEN"
        document["features"][0]["properties"]["private_canary"] = "FEATURE_MUST_NOT_BE_FROZEN"
        source = forbidden / "floorplan.json"
        source.write_text(json.dumps(document), encoding="utf-8")
        operations = self.scratch / "forbidden-floorplan-operations.json"
        operations.write_text(
            json.dumps(
                [
                    [
                        "import-floorplan",
                        "--input",
                        str(source),
                        "--source-ref",
                        "checked source",
                        "--captured-on",
                        "2026-08-06",
                        "--sensitivity",
                        "low",
                    ]
                ]
            ),
            encoding="utf-8",
        )
        rejected = subprocess.run(
            [
                sys.executable,
                str(CLI),
                "--inventory-root",
                str(self.root),
                "--runtime-dir",
                str(self.runtime),
                "--scope",
                "private",
                "--forbidden-root",
                str(forbidden),
                "propose",
                "--operations",
                str(operations),
            ],
            text=True,
            capture_output=True,
            check=False,
        )
        self.assertNotEqual(rejected.returncode, 0)
        self.assertIn("outside forbidden root", json.loads(rejected.stderr)["error"])

        safe_source = self.scratch / "safe-floorplan.json"
        safe_source.write_text(json.dumps(document), encoding="utf-8")
        safe_operations = self.scratch / "safe-floorplan-operations.json"
        safe_operations.write_text(
            operations.read_text(encoding="utf-8").replace(str(source), str(safe_source)),
            encoding="utf-8",
        )
        proposal = self.cli("propose", "--operations", str(safe_operations))["proposal"]
        frozen = proposal["operations"][0]
        canonical = frozen[frozen.index("--document-json") + 1]
        self.assertNotIn("TOP_LEVEL_MUST_NOT_BE_FROZEN", canonical)
        self.assertNotIn("FEATURE_MUST_NOT_BE_FROZEN", canonical)
        self.cli("proposal-apply", proposal["proposal_id"])
        self.assertEqual(self.cli("space", "loc-fixture-public-zone")["status"], "known")

    def test_large_unconsumed_floorplan_content_is_stripped_before_materialization(self) -> None:
        document = json.loads(self.floorplan.read_text(encoding="utf-8"))
        document["features"][0]["properties"]["ignored_padding"] = "x" * 1_100_000
        large_floorplan = self.scratch / "large-floorplan.geojson"
        large_floorplan.write_text(json.dumps(document), encoding="utf-8")
        operations = self.scratch / "large-operations.json"
        operations.write_text(
            json.dumps(
                [
                    [
                        "import-floorplan",
                        "--input",
                        str(large_floorplan),
                        "--source-ref",
                        "Synthetic fixture zone dimensions",
                        "--captured-on",
                        "2026-08-05",
                        "--sensitivity",
                        "low",
                    ]
                ]
            ),
            encoding="utf-8",
        )
        proposal = self.cli("propose", "--operations", str(operations))["proposal"]
        frozen = proposal["operations"][0]
        canonical = frozen[frozen.index("--document-json") + 1]
        self.assertLess(len(canonical), 10_000)
        self.assertNotIn("ignored_padding", canonical)
        self.assertNotIn("x" * 1_000, canonical)
        applied = self.cli("proposal-apply", proposal["proposal_id"])
        self.assertEqual(applied["status"], "committed_to_store")
        self.assertEqual(self.cli("space", "loc-fixture-public-zone")["profiles"][0]["width"], 4.0)

    def test_fit_pack_unknowns_and_real_stdio_mcp_share_the_cli_surface(self) -> None:
        self.add_visible_box()
        fit = self.cli(
            "fit",
            "loc-fixture-public-zone",
            "--item-dimensions",
            '{"width":3,"height":1,"depth":1,"unit":"cm"}',
            scope="public",
        )
        self.assertEqual(fit["status"], "fits")
        self.assertEqual(
            self.cli(
                "fit",
                "loc-fixture-public-zone",
                "--item-dimensions",
                '{"width":3,"unit":"cm"}',
                scope="public",
            )["status"],
            "unknown",
        )
        self.cli_fails(
            "fit",
            "loc-fixture-public-zone",
            "--item-dimensions",
            '{"width":"bad","height":1,"depth":1,"unit":"cm"}',
            scope="public",
        )
        items = [
            {"item_id": "b", "dimensions": {"width": 3, "height": 1, "depth": 1, "unit": "cm"}},
            {"item_id": "a", "dimensions": {"width": 2, "height": 1, "depth": 1, "unit": "cm"}},
        ]
        packed = self.cli(
            "pack", "loc-fixture-public-zone", "--items", json.dumps(items), scope="public"
        )
        self.assertEqual(packed["status"], "partial")
        self.assertEqual([entry["item_id"] for entry in packed["placements"]], ["a"])
        self.assertEqual(packed["placements"][0]["box"]["x"], 10.0)
        self.assertEqual(packed["placements"][0]["box"]["y"], 20.0)
        self.assertEqual(packed["placements"][0]["box"]["z"], 30.0)
        self.assertEqual(packed["unplaced_item_ids"], ["b"])
        unknown = self.cli(
            "fit",
            "loc-fixture-restricted-zone",
            "--item-dimensions",
            '{"width":1,"height":1,"depth":1,"unit":"cm"}',
            scope="public",
        )
        self.assertEqual(
            unknown, {"status": "unknown", "reason": "container_box_not_visible_or_not_recorded"}
        )

        async def scenario() -> None:
            async with Client(stdio_client(self.parameters("public")), mode="legacy") as client:
                tools = await client.list_tools()
                names = {tool.name for tool in tools.tools}
                self.assertTrue(
                    {"get_space_context", "check_spatial_fit", "plan_spatial_packing"} <= names
                )
                context = await client.call_tool(
                    "get_space_context", {"location_id": "loc-fixture-public-zone"}
                )
                self.assertFalse(context.is_error, context.content)
                self.assertEqual(
                    context.structured_content,
                    self.cli("space", "loc-fixture-public-zone", scope="public"),
                )
                checked = await client.call_tool(
                    "check_spatial_fit",
                    {
                        "location_id": "loc-fixture-public-zone",
                        "item_dimensions": {"width": 3, "height": 1, "depth": 1, "unit": "cm"},
                    },
                )
                self.assertFalse(checked.is_error, checked.content)
                self.assertEqual(checked.structured_content, fit)
                planned = await client.call_tool(
                    "plan_spatial_packing",
                    {"location_id": "loc-fixture-public-zone", "items": items},
                )
                self.assertFalse(planned.is_error, planned.content)
                self.assertEqual(planned.structured_content, packed)
                hidden = await client.call_tool(
                    "get_space_context", {"location_id": "loc-fixture-restricted-zone"}
                )
                self.assertFalse(hidden.is_error, hidden.content)
                self.assertEqual(
                    hidden.structured_content,
                    {"status": "unknown", "reason": "space_not_visible_or_not_recorded"},
                )

        asyncio.run(scenario())


if __name__ == "__main__":
    unittest.main()
