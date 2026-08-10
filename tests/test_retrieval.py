"""Golden retrieval tests for scope-safe aliases, filters, context, and MCP parity."""

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

HERE = Path(__file__).resolve().parents[1]
CLI = HERE / "property_inventory.py"
MCP = Path(sys.executable).parent / "property-inventory-mcp"


class RetrievalTest(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory(prefix="inventory-retrieval-")
        self.scratch = Path(self.temp.name)
        self.root = self.scratch / "inventory"
        self.runtime = self.scratch / "runtime"
        self.cli("init")
        self.cli(
            "add-location",
            "--name",
            "Workshop",
            "--location-id",
            "loc-workshop",
            "--kind",
            "room",
            "--sensitivity",
            "low",
        )
        self.cli(
            "add-location",
            "--name",
            "Unspecified",
            "--location-id",
            "loc-unspecified",
            "--kind",
            "unknown",
            "--sensitivity",
            "low",
        )
        self.cli(
            "add-location",
            "--name",
            "Hidden locker",
            "--location-id",
            "loc-hidden-locker",
            "--parent-location-id",
            "loc-workshop",
            "--kind",
            "container",
            "--sensitivity",
            "high",
        )
        self.cli(
            "add-location",
            "--name",
            "Visible case",
            "--location-id",
            "loc-visible-case",
            "--parent-location-id",
            "loc-workshop",
            "--kind",
            "container",
            "--sensitivity",
            "low",
        )
        self.t25 = self.order("T25", "driver bit", sensitivity="low", location="loc-workshop")
        self.second = self.order(
            "T25 spare", "driver bit", sensitivity="low", location="loc-unspecified"
        )
        self.private = self.order(
            "private fixture", "driver bit", sensitivity="high", location="loc-hidden-locker"
        )
        self.cli(
            "receive",
            "--actor",
            "Retrieval test",
            "--source-ref",
            "Private serial check",
            "--item-id",
            self.private["item_id"],
            "--received-on",
            "2026-08-06",
            "--location-unchanged",
            "--serial-or-lot",
            "CANARY-HIDDEN-SERIAL",
        )
        self.add_alias(self.t25, "Torx", "common_name", "low")
        self.add_alias(self.second, "Shared Driver", "common_name", "low")
        self.add_alias(self.t25, "Shared Driver", "common_name", "low")
        self.add_alias(self.private, "CANARY-HIDDEN-ALIAS", "private_label", "high")
        self.t25_model_id = self.cli("show", self.t25["item_id"])["model"]["model_id"]
        self.cli(
            "add-interface",
            "--model-id",
            self.t25_model_id,
            "--evidence-id",
            self.t25["evidence_id"],
            "--family",
            "hex-drive",
            "--standard",
            "1/4 inch",
            "--variant",
            "ISO-1173",
            "--direction",
            "plug",
            "--role",
            "provides",
        )
        self._add_tag(self.t25["item_id"], "fastener")
        self._set_condition(self.t25["item_id"], "new")

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

    def order(
        self, name: str, category: str, *, sensitivity: str, location: str | None = None
    ) -> dict:
        command = [
            "order",
            "--actor",
            "Retrieval test",
            "--source-ref",
            f"Evidence for {name}",
            "--name",
            name,
            "--category",
            category,
            "--ordered-on",
            "2026-08-06",
            "--order-placed",
            "--sensitivity",
            sensitivity,
        ]
        if location is not None:
            command.extend(("--location-id", location))
        return self.cli(*command)["result"]

    def add_alias(self, item: dict, alias: str, alias_kind: str, sensitivity: str) -> None:
        self.cli(
            "add-alias",
            "--item-id",
            item["item_id"],
            "--alias",
            alias,
            "--alias-kind",
            alias_kind,
            "--evidence-id",
            item["evidence_id"],
            "--sensitivity",
            sensitivity,
        )

    def _add_tag(self, item_id: str, tag: str) -> None:
        evidence_id = self.cli("show", item_id)["evidence_ids"][0]
        self.cli(
            "add-tag",
            "--item-id", item_id,
            "--tag", tag,
            "--evidence-id", evidence_id,
            "--sensitivity", "low",
        )

    def _set_condition(self, item_id: str, condition: str) -> None:
        evidence_id = self.cli("show", item_id)["evidence_ids"][0]
        self.cli(
            "enrich-item",
            "--actor", "Retrieval test",
            "--item-id", item_id,
            "--evidence-id", evidence_id,
            "--amended-on", "2026-08-06",
            "--condition", condition,
        )

    def _set_item_locations(
        self, item_id: str, *, location_id: str | None, container_id: str | None
    ) -> None:
        shown = self.cli("show", item_id)
        if shown["item"]["ownership_state"] not in {"confirmed", "lent"}:
            self.cli(
                "physical-check",
                "--actor", "Retrieval test",
                "--source-ref", "Fixture physical check",
                "--item-id", item_id,
                "--checked-on", "2026-08-07",
                "--location-id", "loc-workshop",
            )
        command = [
            "move",
            "--actor", "Retrieval test",
            "--source-ref", "Fixture location update",
            "--item-id", item_id,
            "--moved-on", "2026-08-08",
            "--location-id", location_id or "loc-unknown",
        ]
        if container_id is not None:
            command.extend(("--container-id", container_id))
        self.cli(*command)

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

    def test_alias_is_evidence_backed_case_and_punctuation_insensitive(self) -> None:
        result = self.cli("search", "tOrX!!!", scope="public")
        self.assertEqual(result["count"], 1)
        self.assertEqual(result["matches"][0]["item"]["item_id"], self.t25["item_id"])
        self.assertEqual(result["matches"][0]["model"]["name"], "T25")
        self.assertEqual(
            self.cli("show", "TORX", scope="public")["item"]["item_id"], self.t25["item_id"]
        )
        self.assertFalse(self.cli("search", "not recorded", scope="public")["recorded"])
        self.assertEqual(
            self.cli("search", "not recorded", scope="public")["meaning_if_empty"],
            "unknown, not absent",
        )

    def test_search_summary_keeps_known_facts_and_unknown_meaning(self) -> None:
        self.assertEqual(
            self.cli("search", "Torx", "--summary", scope="public"),
            {
                "matching_record_found": True,
                "count": 1,
                "matches": [
                    {
                        "name": "T25",
                        "ownership": "candidate",
                        "condition": "new",
                        "location": "Workshop",
                        "last_physical_check_on": None,
                        "evidence_types": ["merchant_account"],
                    }
                ],
                "next_cursor": None,
                "page_count": 1,
                "truncated": False,
            },
        )
        self.assertEqual(
            self.cli("search", "not recorded", "--summary", scope="public"),
            {
                "matching_record_found": False,
                "count": 0,
                "matches": [],
                "meaning": "unknown, not absent",
                "next_cursor": None,
                "page_count": 0,
                "truncated": False,
            },
        )
        completed = subprocess.run(
            self.command("search", "not recorded", "--summary", scope="public"),
            text=True,
            capture_output=True,
            check=False,
        )
        self.assertEqual(completed.returncode, 0, completed.stderr or completed.stdout)
        self.assertLess(
            completed.stdout.index('"meaning"'),
            completed.stdout.index('"matching_record_found"'),
        )

        first_page = self.cli("search", "T25", "--limit", "1", "--summary")
        self.assertEqual(first_page["page_count"], 1)
        self.assertTrue(first_page["truncated"])
        self.assertIsInstance(first_page["next_cursor"], str)
        second_page = self.cli(
            "search",
            "T25",
            "--limit",
            "1",
            "--cursor",
            first_page["next_cursor"],
            "--summary",
        )
        self.assertEqual(second_page["page_count"], 1)
        self.assertFalse(second_page["truncated"])
        self.assertIsNone(second_page["next_cursor"])
        self.assertEqual(
            {first_page["matches"][0]["name"], second_page["matches"][0]["name"]},
            {"T25", "T25 spare"},
        )

    def test_status_summary_reports_the_integrity_gate(self) -> None:
        self.assertEqual(
            self.cli("status", "--summary"),
            {
                "integrity_gate": "pass",
                "verification_failures": [],
                "foreign_key_failures": 0,
            },
        )
        for scope in ("public", "personal"):
            self.assertEqual(
                self.cli("status", "--summary", scope=scope),
                {
                    "integrity_gate": "pass",
                    "scope": scope,
                    "verification_failures": None,
                    "foreign_key_failures": None,
                },
            )

    def test_typed_filters_and_deterministic_limit(self) -> None:
        common = (
            "--category",
            "driver-bit",
            "--ownership-state",
            "candidate",
            "--condition",
            "NEW",
        )
        result = self.cli(
            "search",
            "t25",
            *common,
            "--tag",
            "FASTENER",
            "--alias-kind",
            "common-name",
            "--interface-family",
            "hex drive",
            "--interface-standard",
            "1/4-inch",
            "--interface-variant",
            "iso 1173",
            "--interface-direction",
            "plug",
            "--location",
            "workshop",
            "--location-known",
            "known",
            scope="public",
        )
        self.assertEqual([match["item"]["item_id"] for match in result["matches"]], [self.t25["item_id"]])
        limited = self.cli("search", "t25", "--limit", "1", scope="public")
        all_matches = self.cli("search", "t25", scope="public")
        self.assertEqual(limited["count"], all_matches["count"])
        self.assertEqual(
            [match["item"]["item_id"] for match in limited["matches"]],
            sorted(match["item"]["item_id"] for match in all_matches["matches"])[:1],
        )
        self.assertEqual(
            self.cli("search", "t25", "--location-known", "unknown", scope="personal")["count"],
            1,
        )
        self.assertEqual(
            self.cli("search", "t25", "--location-known", "unknown", scope="public")["count"],
            1,
        )

    def test_lower_scope_never_matches_or_counts_private_alias_serial_or_location(self) -> None:
        for query in (
            "CANARY-HIDDEN-ALIAS",
            "CANARY-HIDDEN-SERIAL",
            "Hidden locker",
            "loc hidden locker",
        ):
            with self.subTest(query=query):
                result = self.cli("search", query, scope="public")
                self.assertEqual(result["count"], 0)
                self.assertEqual(result["matches"], [])
        self.assertEqual(
            self.cli("search", "private fixture", scope="public")["count"], 0
        )

    def test_location_state_uses_container_unknown_and_scope_redaction(self) -> None:
        semantic_unknown = self.cli(
            "search", "t25 spare", "--location-known", "unknown", scope="personal"
        )
        self.assertEqual(
            [match["item"]["item_id"] for match in semantic_unknown["matches"]],
            [self.second["item_id"]],
        )
        self._set_item_locations(
            self.second["item_id"],
            location_id="loc-workshop",
            container_id="loc-visible-case",
        )
        visible_container = self.cli(
            "search", "t25 spare", "--location-known", "known", scope="personal"
        )
        self.assertEqual(
            [match["item"]["item_id"] for match in visible_container["matches"]],
            [self.second["item_id"]],
        )
        self._set_item_locations(
            self.t25["item_id"],
            location_id="loc-hidden-locker",
            container_id=None,
        )
        for state in ("known", "unknown"):
            with self.subTest(state=state):
                self.assertEqual(
                    self.cli(
                        "search", "torx", "--location-known", state, scope="personal"
                    )["count"],
                    0,
                )
        redacted = self.cli("search", "torx", scope="personal")
        self.assertEqual(redacted["count"], 1)
        self.assertEqual(redacted["matches"][0]["location"], "[redacted]")

    def test_show_refuses_ambiguous_alias_and_context_names_unknowns(self) -> None:
        failed = self.cli_fails("show", "shared-driver", scope="private")
        self.assertIn("alias is ambiguous", failed["error"])
        context = self.cli("context", "--task", "torx", scope="public")
        self.assertTrue(context["recorded"])
        self.assertEqual(context["meaning_if_empty"], "unknown, not absent")
        self.assertEqual(
            context["unknowns"],
            [{"item_id": self.t25["item_id"], "fields": ["container", "serial_or_lot"]}],
        )
        empty = self.cli("context", "--task", "not-recorded-task", scope="public")
        self.assertEqual(empty["unknowns"][0]["meaning"], "unknown, not absent")

    def test_add_alias_rejects_unlinked_evidence_and_works_in_a_proposal(self) -> None:
        failed = self.cli_fails(
            "add-alias",
            "--item-id",
            self.t25["item_id"],
            "--alias",
            "invalid evidence",
            "--alias-kind",
            "label",
            "--evidence-id",
            self.second["evidence_id"],
            "--sensitivity",
            "low",
        )
        self.assertIn("must already support", failed["error"])
        floor = self.cli_fails(
            "add-alias",
            "--item-id",
            self.private["item_id"],
            "--alias",
            "under-classified",
            "--alias-kind",
            "label",
            "--evidence-id",
            self.private["evidence_id"],
            "--sensitivity",
            "low",
        )
        self.assertIn("sensitivity must be at least", floor["error"])
        operations = self.scratch / "alias-proposal.json"
        operations.write_text(
            json.dumps(
                [[
                    "add-alias",
                    "--item-id",
                    self.second["item_id"],
                    "--alias",
                    "proposal alias",
                    "--alias-kind",
                    "label",
                    "--evidence-id",
                    self.second["evidence_id"],
                    "--sensitivity",
                    "low",
                ]]
            ),
            encoding="utf-8",
        )
        proposal = self.cli("propose", "--operations", str(operations))["proposal"]
        self.cli("proposal-apply", proposal["proposal_id"])
        self.assertEqual(
            self.cli("search", "proposal alias", scope="public")["count"], 1
        )

    def test_real_stdio_mcp_matches_cli_exactly(self) -> None:
        cli_result = self.cli(
            "search",
            "torx",
            "--category",
            "driver bit",
            "--alias-kind",
            "common name",
            "--interface-family",
            "hex drive",
            "--location-known",
            "known",
            scope="public",
        )

        async def scenario() -> dict:
            async with Client(stdio_client(self.parameters("public")), mode="legacy") as client:
                response = await client.call_tool(
                    "search_inventory",
                    {
                        "query": "torx",
                        "category": "driver bit",
                        "alias_kind": "common name",
                        "interface_family": "hex drive",
                        "location_known": "known",
                    },
                )
                self.assertFalse(response.is_error, response.content)
                context = await client.call_tool("inventory_task_context", {"task": "torx"})
                self.assertFalse(context.is_error, context.content)
                self.assertEqual(context.structured_content["meaning_if_empty"], "unknown, not absent")
                return response.structured_content

        self.assertEqual(asyncio.run(scenario()), cli_result)

        cli_two_words = self.cli(
            "search", "t25", "spare", "--location-known", "unknown", scope="public"
        )

        async def two_word_scenario() -> dict:
            async with Client(stdio_client(self.parameters("public")), mode="legacy") as client:
                response = await client.call_tool(
                    "search_inventory",
                    {"query": "t25 spare", "location_known": "unknown"},
                )
                self.assertFalse(response.is_error, response.content)
                return response.structured_content

        self.assertEqual(asyncio.run(two_word_scenario()), cli_two_words)

    def test_malformed_mcp_filters_are_safe_errors_and_session_survives(self) -> None:
        async def scenario() -> None:
            async with Client(stdio_client(self.parameters("public")), mode="legacy") as client:
                for arguments in (
                    {"query": "torx", "interface_direction": "sideways"},
                    {"query": "torx", "location_known": "maybe"},
                ):
                    rejected = await client.call_tool("search_inventory", arguments)
                    self.assertTrue(rejected.is_error)
                    rendered = str(rejected.content).casefold()
                    self.assertNotIn("usage:", rendered)
                    self.assertNotIn(str(self.root).casefold(), rendered)
                alive = await client.call_tool("search_inventory", {"query": "torx"})
                self.assertFalse(alive.is_error, alive.content)
                self.assertEqual(alive.structured_content["count"], 1)

        asyncio.run(scenario())


if __name__ == "__main__":
    unittest.main()
