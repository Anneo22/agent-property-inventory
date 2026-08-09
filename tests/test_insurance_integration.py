"""Live CLI and stdio MCP coverage for private insurance preparation."""

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
from PIL import Image, PngImagePlugin

from property_inventory.insurance import MAX_INSURANCE_PACKAGE_BYTES

HERE = Path(__file__).resolve().parents[1]
CLI = HERE / "property_inventory.py"
MCP = Path(sys.executable).parent / "property-inventory-mcp"


class InsuranceIntegrationTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory(prefix="inventory-insurance-integration-")
        self.scratch = Path(self.temp.name)
        self.root = self.scratch / "inventory"
        self.runtime = self.scratch / "runtime"
        self.media = self.scratch / "media"
        self.cli("init")
        self.public = self.record_item("Public camera", "low", "PUBLIC-EVIDENCE")
        self.personal = self.record_item("Personal camera", "personal", "PERSONAL-CANARY")
        self.private = self.record_item("Private camera", "high", "PRIVATE-CANARY")

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
            "--media-root",
            str(self.media),
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

    def failed(self, *arguments: str, scope: str = "private") -> dict:
        completed = subprocess.run(
            self.command(*arguments, scope=scope), text=True, capture_output=True, check=False
        )
        self.assertNotEqual(completed.returncode, 0, completed.stdout)
        return json.loads(completed.stderr)

    def record_item(self, name: str, sensitivity: str, media_content: str) -> dict:
        location_id = f"loc-{sensitivity}-{name.casefold().replace(' ', '-')}"
        self.cli(
            "add-location",
            "--location-id",
            location_id,
            "--name",
            f"{name} shelf",
            "--kind",
            "room",
            "--sensitivity",
            sensitivity,
        )
        ordered = self.cli(
            "order",
            "--actor",
            "Insurance integration test",
            "--source-ref",
            f"Order evidence for {name}",
            "--name",
            name,
            "--category",
            "electronics",
            "--ordered-on",
            "2026-08-06",
            "--order-placed",
            "--location-id",
            location_id,
            "--sensitivity",
            sensitivity,
        )["result"]
        received = self.cli(
            "receive",
            "--actor",
            "Insurance integration test",
            "--source-ref",
            f"Physical check for {name}",
            "--item-id",
            ordered["item_id"],
            "--received-on",
            "2026-08-06",
            "--location-id",
            location_id,
            "--serial-or-lot",
            f"SERIAL-{sensitivity}",
            "--physical-check",
        )["result"]
        source = self.scratch / f"{sensitivity}-{name.casefold().replace(' ', '-')}.png"
        metadata = PngImagePlugin.PngInfo()
        metadata.add_text("fixture", media_content)
        Image.new("RGB", (8, 6), (20, 30, 40)).save(source, format="PNG", pnginfo=metadata)
        attached = self.cli(
            "attach-media",
            "--evidence-id",
            received["evidence_id"],
            "--file",
            str(source),
            "--role",
            "source",
            "--media-type",
            "image/png",
            "--sensitivity",
            sensitivity,
        )["result"]
        return {
            **ordered,
            **received,
            **attached,
            "order_evidence_id": ordered["evidence_id"],
            "physical_evidence_id": received["evidence_id"],
        }

    def parameters(self, scope: str, profile: str = "read") -> StdioServerParameters:
        self.assertTrue(MCP.is_file(), f"MCP console script not installed: {MCP}")
        return StdioServerParameters(
            command=str(MCP),
            args=[
                "--inventory-root",
                str(self.root),
                "--runtime-dir",
                str(self.runtime),
                "--media-root",
                str(self.media),
                "--scope",
                scope,
                "--profile",
                profile,
            ],
            cwd=self.scratch,
        )

    def test_status_is_scope_filtered_and_keeps_missing_values_unknown(self) -> None:
        public = self.cli("insurance-status", scope="public")
        personal = self.cli("insurance-status", scope="personal")
        private = self.cli("insurance-status", scope="private")
        self.assertEqual(public["summary"]["item_count"], 1)
        self.assertEqual(personal["summary"]["item_count"], 2)
        self.assertEqual(private["summary"]["item_count"], 3)
        public_serialized = json.dumps(public, sort_keys=True).casefold()
        personal_serialized = json.dumps(personal, sort_keys=True).casefold()
        self.assertNotIn("personal-canary", public_serialized)
        self.assertNotIn("private-canary", public_serialized)
        self.assertNotIn("private-canary", personal_serialized)
        self.assertEqual(public["items"][0]["fields"]["value"], {"state": "unknown"})
        self.assertIn("value", public["items"][0]["gaps"])

    def test_insurance_media_roles_require_matching_evidence_and_real_bytes(self) -> None:
        arbitrary = self.scratch / "not-a-receipt.txt"
        arbitrary.write_text("ordinary text, not a receipt or image", encoding="utf-8")
        wrong_claim = self.failed(
            "attach-media",
            "--evidence-id",
            self.public["physical_evidence_id"],
            "--file",
            str(arbitrary),
            "--role",
            "receipt",
            "--media-type",
            "text/plain",
            "--sensitivity",
            "low",
        )
        self.assertIn("purchase evidence", wrong_claim["error"])

        wrong_bytes = self.failed(
            "attach-media",
            "--evidence-id",
            self.public["physical_evidence_id"],
            "--file",
            str(arbitrary),
            "--role",
            "source",
            "--media-type",
            "image/jpeg",
            "--sensitivity",
            "low",
        )
        self.assertIn("declared role", wrong_bytes["error"])

        wrong_document = self.failed(
            "attach-media",
            "--evidence-id",
            self.public["order_evidence_id"],
            "--file",
            str(arbitrary),
            "--role",
            "receipt",
            "--media-type",
            "text/plain",
            "--sensitivity",
            "low",
        )
        self.assertIn("must be an image or PDF", wrong_document["error"])

        receipt = self.scratch / "receipt.png"
        Image.new("RGB", (5, 5), (90, 100, 110)).save(receipt, format="PNG")
        self.cli(
            "attach-media",
            "--evidence-id",
            self.public["order_evidence_id"],
            "--file",
            str(receipt),
            "--role",
            "receipt",
            "--media-type",
            "image/png",
            "--sensitivity",
            "low",
        )
        fields = self.cli("insurance-status", scope="public")["items"][0]["fields"]
        self.assertEqual(fields["photo"]["state"], "present")
        self.assertEqual(fields["receipt"], {"state": "present"})

    def test_rebuild_rejects_a_receipt_role_spliced_onto_physical_evidence(self) -> None:
        links = self.root / "Data" / "store" / "evidence_assets.jsonl"
        with links.open("a", encoding="utf-8") as handle:
            handle.write(
                json.dumps(
                    {
                        "evidence_id": self.public["physical_evidence_id"],
                        "asset_id": self.public["asset_id"],
                        "role": "receipt",
                        "region_json": None,
                    },
                    sort_keys=True,
                )
                + "\n"
            )
        error = self.failed("status")
        self.assertIn("receipt media lacks purchase-document evidence semantics", error["error"])

    def test_rebuild_rejects_an_appraisal_value_spliced_onto_physical_evidence(self) -> None:
        valuations = self.root / "Data" / "store" / "valuations.jsonl"
        with valuations.open("a", encoding="utf-8") as handle:
            handle.write(
                json.dumps(
                    {
                        "valuation_id": "valuation-forged-appraisal",
                        "item_id": self.public["item_id"],
                        "amount": 500,
                        "currency": "GBP",
                        "valued_on": "2026-08-06",
                        "basis": "appraisal",
                        "evidence_id": self.public["physical_evidence_id"],
                        "sensitivity": "low",
                        "notes": None,
                    },
                    sort_keys=True,
                )
                + "\n"
            )
        error = self.failed("status")
        self.assertIn("valuation evidence has incompatible semantics", error["error"])

    def test_evidence_backed_valuations_are_writable_idempotent_and_proposal_safe(self) -> None:
        arguments = (
            "add-valuation",
            "--item-id",
            self.public["item_id"],
            "--amount",
            "1450",
            "--currency",
            "GBP",
            "--valued-on",
            "2026-08-06",
            "--basis",
            "replacement",
            "--source-ref",
            "Checked replacement quote",
            "--captured-on",
            "2026-08-06",
            "--evidence-type",
            "research",
            "--sensitivity",
            "low",
        )
        created = self.cli(*arguments)["result"]
        self.assertTrue(created["evidence_created"])
        self.assertFalse(created["valuation_reused"])
        retried = self.cli(*arguments)["result"]
        self.assertFalse(retried["evidence_created"])
        self.assertTrue(retried["valuation_reused"])
        readiness = self.cli("insurance-status", scope="public")
        value = readiness["items"][0]["fields"]["value"]
        self.assertEqual(value["state"], "present")
        self.assertEqual(value["valuations"][0]["amount"], 1450.0)

        appraisal_evidence = self.cli(
            "record-evidence",
            "--item-id",
            self.personal["item_id"],
            "--source-ref",
            "Reviewed signed appraisal fixture",
            "--captured-on",
            "2026-08-06",
            "--evidence-type",
            "user_source",
            "--claim-strength",
            "research_only",
            "--sensitivity",
            "personal",
        )["result"]["evidence_id"]
        appraisal_file = self.scratch / "appraisal.png"
        Image.new("RGB", (6, 4), (50, 60, 70)).save(appraisal_file, format="PNG")
        self.cli(
            "attach-media",
            "--evidence-id",
            appraisal_evidence,
            "--file",
            str(appraisal_file),
            "--role",
            "appraisal",
            "--media-type",
            "image/png",
            "--sensitivity",
            "personal",
        )
        operations = self.scratch / "valuation-operations.json"
        operations.write_text(
            json.dumps(
                [
                    [
                        "add-valuation",
                        "--item-id",
                        self.personal["item_id"],
                        "--amount",
                        "900",
                        "--currency",
                        "GBP",
                        "--valued-on",
                        "2026-08-06",
                        "--basis",
                        "appraisal",
                        "--evidence-id",
                        appraisal_evidence,
                        "--sensitivity",
                        "personal",
                    ]
                ]
            ),
            encoding="utf-8",
        )
        proposal = self.cli("propose", "--operations", str(operations))["proposal"]
        self.cli("proposal-apply", proposal["proposal_id"])
        personal = self.cli("insurance-status", scope="personal")
        personal_item = next(
            item for item in personal["items"] if item["item_id"] == self.personal["item_id"]
        )
        self.assertEqual(personal_item["fields"]["value"]["state"], "present")
        self.assertEqual(personal_item["fields"]["appraisal"]["state"], "present")

        invalid_appraisal = self.failed(
            "add-valuation",
            "--item-id",
            self.public["item_id"],
            "--amount",
            "123",
            "--currency",
            "GBP",
            "--valued-on",
            "2026-08-06",
            "--basis",
            "appraisal",
            "--evidence-id",
            self.public["physical_evidence_id"],
            "--sensitivity",
            "low",
        )
        self.assertIn("incompatible type or claim strength", invalid_appraisal["error"])

        unlinked = self.failed(
            "add-valuation",
            "--item-id",
            self.public["item_id"],
            "--amount",
            "1",
            "--currency",
            "GBP",
            "--valued-on",
            "2026-08-06",
            "--basis",
            "replacement",
            "--evidence-id",
            self.personal["evidence_id"],
            "--sensitivity",
            "personal",
        )
        self.assertIn("must already support", unlinked["error"])
        too_low = self.failed(
            "add-valuation",
            "--item-id",
            self.private["item_id"],
            "--amount",
            "1",
            "--currency",
            "GBP",
            "--valued-on",
            "2026-08-06",
            "--basis",
            "replacement",
            "--source-ref",
            "Low-scope quote",
            "--captured-on",
            "2026-08-06",
            "--sensitivity",
            "personal",
        )
        self.assertIn("sensitivity", too_low["error"])
        self.assertIn(
            "finite and non-negative",
            self.failed(
                "add-valuation",
                "--item-id",
                self.public["item_id"],
                "--amount",
                "inf",
                "--currency",
                "GBP",
                "--valued-on",
                "2026-08-06",
                "--basis",
                "replacement",
                "--evidence-id",
                self.public["evidence_id"],
                "--sensitivity",
                "low",
            )["error"],
        )
        self.assertEqual(self.cli("status")["verification"]["failures"], [])

    def test_private_package_is_deterministic_validated_and_fails_closed_on_media_or_zip_tampering(
        self,
    ) -> None:
        first = self.scratch / "first-insurance.zip"
        second = self.scratch / "second-insurance.zip"
        exported = self.cli("insurance-export", "--output", str(first))
        self.cli("insurance-export", "--output", str(second))
        self.assertEqual(first.read_bytes(), second.read_bytes())
        validated = self.cli("insurance-validate", "--package", str(first))
        self.assertEqual(validated["report"], exported["report"])
        self.assertEqual(validated["report"]["summary"]["item_count"], 3)

        corrupt = self.scratch / "corrupt-insurance.zip"
        payload = bytearray(first.read_bytes())
        marker = payload.index(b"PUBLIC-EVIDENCE")
        payload[marker] ^= 1
        corrupt.write_bytes(payload)
        error = self.failed("insurance-validate", "--package", str(corrupt))
        self.assertIn("cannot validate insurance package", error["error"])

        media_path = Path(self.public["media_path"])
        original_media = media_path.read_bytes()
        media_path.write_bytes(b"tampered")
        readiness = self.cli("insurance-status", scope="public")
        self.assertEqual(readiness["items"][0]["fields"]["photo"], {"state": "unknown"})
        self.assertIn("photo", readiness["items"][0]["gaps"])
        missing_package = self.scratch / "must-not-exist.zip"
        error = self.failed("insurance-export", "--output", str(missing_package))
        self.assertIn("tampered", error["error"])
        self.assertFalse(missing_package.exists())
        media_path.write_bytes(original_media)

    def test_export_rejects_digest_correct_bytes_with_a_forged_media_type(self) -> None:
        payload = b"plain text deliberately mislabeled as an image"
        digest = hashlib.sha256(payload).hexdigest()
        media_path = self.media / "sha256" / digest[:2] / digest
        media_path.parent.mkdir(parents=True, exist_ok=True)
        media_path.write_bytes(payload)
        asset_id = "asset-forged-media-type"
        with (self.root / "Data" / "store" / "media_assets.jsonl").open(
            "a", encoding="utf-8"
        ) as handle:
            handle.write(
                json.dumps(
                    {
                        "asset_id": asset_id,
                        "byte_size": len(payload),
                        "captured_on": "2026-08-06",
                        "media_type": "image/png",
                        "original_name": "forged.png",
                        "sensitivity": "low",
                        "sha256": digest,
                        "uri": f"media://sha256/{digest}",
                    },
                    sort_keys=True,
                )
                + "\n"
            )
        with (self.root / "Data" / "store" / "evidence_assets.jsonl").open(
            "a", encoding="utf-8"
        ) as handle:
            handle.write(
                json.dumps(
                    {
                        "asset_id": asset_id,
                        "evidence_id": self.public["physical_evidence_id"],
                        "region_json": None,
                        "role": "source",
                    },
                    sort_keys=True,
                )
                + "\n"
            )
        readiness = self.cli("status")
        self.assertEqual(readiness["verification"]["failures"], [])
        output = self.scratch / "forged-type-must-not-exist.zip"
        error = self.failed("insurance-export", "--output", str(output))
        self.assertIn("declared media type", error["error"])
        self.assertFalse(output.exists())

    def test_package_commands_require_private_scope_and_a_fresh_safe_destination(self) -> None:
        output = self.scratch / "scope-denied.zip"
        error = self.failed("insurance-export", "--output", str(output), scope="personal")
        self.assertEqual(
            error["error"], "inventory command could not complete safely in this scope"
        )
        self.assertFalse(output.exists())
        valid = self.scratch / "valid.zip"
        self.cli("insurance-export", "--output", str(valid))
        error = self.failed("insurance-validate", "--package", str(valid), scope="public")
        self.assertEqual(
            error["error"], "inventory command could not complete safely in this scope"
        )
        error = self.failed("insurance-export", "--output", str(valid))
        self.assertIn("refusing to overwrite", error["error"])
        error = self.failed("insurance-export", "--output", str(self.media / "inside-media.zip"))
        self.assertIn("outside the media namespace", error["error"])

        oversized = self.scratch / "oversized.zip"
        with oversized.open("wb") as handle:
            handle.truncate(MAX_INSURANCE_PACKAGE_BYTES + 1)
        error = self.failed("insurance-validate", "--package", str(oversized))
        self.assertIn("byte limit", error["error"])

    def test_stdio_mcp_exposes_only_scope_safe_readiness(self) -> None:
        async def scenario() -> None:
            async with Client(stdio_client(self.parameters("public")), mode="legacy") as client:
                listed = await client.list_tools()
                names = {tool.name for tool in listed.tools}
                self.assertIn("get_insurance_readiness", names)
                self.assertFalse(
                    any("insurance" in name and name != "get_insurance_readiness" for name in names)
                )
                result = await client.call_tool("get_insurance_readiness", {})
                self.assertFalse(result.is_error, result.content)
                self.assertEqual(
                    result.structured_content, self.cli("insurance-status", scope="public")
                )
                self.assertNotIn("private-canary", json.dumps(result.structured_content).casefold())

            async with Client(
                stdio_client(self.parameters("private", "write")), mode="legacy"
            ) as client:
                listed = await client.list_tools()
                names = {tool.name for tool in listed.tools}
                self.assertIn("get_insurance_readiness", names)
                self.assertFalse(
                    any("insurance" in name and name != "get_insurance_readiness" for name in names)
                )
                result = await client.call_tool("get_insurance_readiness", {})
                self.assertFalse(result.is_error, result.content)
                self.assertEqual(result.structured_content["summary"]["item_count"], 3)

        asyncio.run(scenario())

    def test_lower_scope_status_failure_never_leaks_private_diagnostics(self) -> None:
        private_media = Path(self.private["media_path"])
        private_bytes = private_media.read_bytes()
        private_media.unlink()
        try:

            async def scenario() -> None:
                async with Client(stdio_client(self.parameters("public")), mode="legacy") as client:
                    result = await client.call_tool("inventory_status", {})
                    self.assertFalse(result.is_error, result.content)
                    self.assertEqual(
                        result.structured_content,
                        {
                            "status": "unhealthy",
                            "scope": "public",
                            "store_valid": False,
                            "recovery": "unknown",
                        },
                    )
                    serialized = json.dumps(result.structured_content).casefold()
                    self.assertNotIn("private-canary", serialized)
                    self.assertNotIn(str(self.root).casefold(), serialized)
                    self.assertNotIn(self.private["sha256"].casefold(), serialized)

            asyncio.run(scenario())
            private_error = self.failed("status", scope="private")["error"]
            self.assertIn("media", private_error)
        finally:
            private_media.write_bytes(private_bytes)


if __name__ == "__main__":
    unittest.main()
