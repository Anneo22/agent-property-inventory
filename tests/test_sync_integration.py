"""Live CLI/MCP integration coverage for the private offline replica workflow."""

from __future__ import annotations

import asyncio
import copy
import json
import os
import shutil
import stat
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

from mcp.client import Client
from mcp.client.stdio import StdioServerParameters, stdio_client
from PIL import Image

from property_inventory.sync import build_store_snapshot

HERE = Path(__file__).resolve().parents[1]
CLI = HERE / "property_inventory.py"
MCP = Path(sys.executable).parent / "property-inventory-mcp"


class SyncIntegrationTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory(prefix="inventory-sync-integration-")
        self.scratch = Path(self.temp.name)
        self.root = self.scratch / "inventory"
        self.runtime = self.scratch / "runtime"
        self.media = self.scratch / "media"
        self.catalogue = self.scratch / "Inventory.md"
        self.cli("init")

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
            "--media-root",
            str(self.media),
            "--catalogue-output",
            str(self.catalogue),
            "--scope",
            scope,
            *arguments,
        ]

    def cli(self, *arguments: str, scope: str = "private") -> dict:
        complete = subprocess.run(
            self.command(*arguments, scope=scope), text=True, capture_output=True, check=False
        )
        self.assertEqual(complete.returncode, 0, complete.stderr or complete.stdout)
        return json.loads(complete.stdout)

    def failed(self, *arguments: str, scope: str = "private") -> dict:
        complete = subprocess.run(
            self.command(*arguments, scope=scope), text=True, capture_output=True, check=False
        )
        self.assertNotEqual(complete.returncode, 0, complete.stdout)
        return json.loads(complete.stderr)

    def snapshot(self, name: str) -> Path:
        path = self.scratch / name
        self.cli("sync-snapshot", "--output", str(path))
        return path

    def tables(self, snapshot: Path) -> dict:
        return json.loads(snapshot.read_text(encoding="utf-8"))["tables"]

    def write_snapshot(self, name: str, tables: dict) -> Path:
        path = self.scratch / name
        path.write_text(json.dumps(build_store_snapshot(tables)), encoding="utf-8")
        return path

    def bundle(self, base: Path, head: Path, name: str = "replica.json") -> Path:
        path = self.scratch / name
        self.cli(
            "sync-bundle", "--replica-ref", "test-replica", "--base", str(base),
            "--head", str(head), "--output", str(path),
        )
        return path

    def amend_location_notes(self, location_id: str, notes: str) -> None:
        """Make a real verified canonical edit instead of bypassing transactions."""
        current = next(
            json.loads(line)
            for line in self.store.joinpath("locations.jsonl").read_text().splitlines()
            if json.loads(line)["location_id"] == location_id
        )
        evidence_id = self.cli(
            "record-evidence",
            "--source-ref",
            f"sync conflict fixture for {location_id}",
            "--captured-on",
            "2026-08-06",
            "--evidence-type",
            "user_source",
            "--claim-strength",
            "research_only",
            "--sensitivity",
            "low",
        )["result"]["evidence_id"]
        replacement = {**current, "notes": notes}
        self.cli(
            "amend-fact",
            "--actor",
            "sync integration fixture",
            "--table",
            "locations",
            "--selector",
            json.dumps({"location_id": location_id}),
            "--action",
            "replace",
            "--replacement",
            json.dumps(replacement),
            "--evidence-id",
            evidence_id,
            "--amended-on",
            "2026-08-06",
            "--reason",
            "exercise a verified sync conflict",
        )

    def replica_location_note_branch(
        self, base_tables: dict, location_id: str, notes: str
    ) -> dict:
        """Build a verifier-valid offline branch, including its fact history."""
        branch = copy.deepcopy(base_tables)
        current = next(
            row for row in branch["locations"] if row["location_id"] == location_id
        )
        previous = dict(current)
        current["notes"] = notes
        evidence_id = f"ev-replica-{location_id}"
        branch["evidence"].append(
            {
                "captured_on": "2026-08-06",
                "claim_strength": "research_only",
                "evidence_id": evidence_id,
                "evidence_type": "user_source",
                "notes": None,
                "sensitivity": "low",
                "source_ref": f"offline replica amendment for {location_id}",
            }
        )
        branch["fact_amendments"].append(
            {
                "action": "replace",
                "actor": "offline replica fixture",
                "amended_on": "2026-08-06",
                "evidence_id": evidence_id,
                "fact_amendment_id": f"fact-amend-replica-{location_id}",
                "notes": None,
                "previous_json": json.dumps(previous, sort_keys=True),
                "reason": "verified offline branch edit",
                "recorded_at": "2026-08-06T00:00:00+00:00",
                "replacement_json": json.dumps(current, sort_keys=True),
                "selector_json": json.dumps({"location_id": location_id}, sort_keys=True),
                "sensitivity": "low",
                "table_name": "locations",
            }
        )
        return branch

    def test_disjoint_merge_is_verified_applied_and_receipted_without_lifecycle_inference(self) -> None:
        base = self.snapshot("base.json")
        replica_tables = self.tables(base)
        replica_tables["locations"].append(
            {"location_id": "loc-replica", "name": "Replica cupboard", "kind": "room", "parent_location_id": None, "sensitivity": "low", "notes": None}
        )
        replica = self.write_snapshot("replica-head.json", replica_tables)
        self.cli("add-location", "--location-id", "loc-canonical", "--name", "Canonical shelf", "--kind", "room", "--sensitivity", "low")
        prepared = self.cli(
            "sync-prepare", "--bundle", str(self.bundle(base, replica)),
            "--trusted-base", str(base),
        )
        self.assertEqual(prepared["status"], "ready")
        applied = self.cli("sync-apply", prepared["plan_id"])
        self.assertEqual(applied["status"], "committed_to_store")
        locations = self.store.joinpath("locations.jsonl").read_text(encoding="utf-8")
        self.assertIn("loc-replica", locations)
        self.assertIn("loc-canonical", locations)
        receipts = [json.loads(line) for line in self.store.joinpath("sync_receipts.jsonl").read_text().splitlines()]
        self.assertEqual(len(receipts), 1)
        evidence = [json.loads(line) for line in self.store.joinpath("evidence.jsonl").read_text().splitlines()]
        receipt_evidence = next(row for row in evidence if row["evidence_id"] == receipts[0]["evidence_id"])
        self.assertEqual((receipt_evidence["evidence_type"], receipt_evidence["claim_strength"]), ("research", "research_only"))
        self.assertEqual(self.store.joinpath("inventory_events.jsonl").read_text(), "")
        self.assertEqual(self.cli("status")["status"], "pass")

    def test_tampered_and_stale_bundles_fail_before_a_canonical_write(self) -> None:
        base = self.snapshot("base.json")
        replica = self.write_snapshot("replica-head.json", self.tables(base))
        bundle = self.bundle(base, replica)
        tampered = json.loads(bundle.read_text())
        tampered["head"]["tables"]["locations"].append(
            {"location_id": "loc-tampered", "name": "Tampered", "kind": "room", "parent_location_id": None, "sensitivity": "low", "notes": None}
        )
        bundle.write_text(json.dumps(tampered), encoding="utf-8")
        before = self.store.joinpath("locations.jsonl").read_bytes()
        self.assertIn(
            "digest",
            self.failed(
                "sync-prepare", "--bundle", str(bundle), "--trusted-base", str(base)
            )["error"],
        )
        self.assertEqual(before, self.store.joinpath("locations.jsonl").read_bytes())
        clean_bundle = self.bundle(base, replica, "clean-replica.json")
        self.cli("add-location", "--location-id", "loc-new", "--name", "New", "--kind", "room", "--sensitivity", "low")
        prepared = self.cli(
            "sync-prepare", "--bundle", str(clean_bundle), "--trusted-base", str(base)
        )
        self.cli("add-location", "--location-id", "loc-later", "--name", "Later", "--kind", "room", "--sensitivity", "low")
        self.assertIn("stale", self.failed("sync-apply", prepared["plan_id"])["error"])

    def test_snapshot_digest_and_independent_trusted_base_are_mandatory(self) -> None:
        base = self.snapshot("base.json")
        self.assertEqual(stat.S_IMODE(base.stat().st_mode), 0o600)
        replica = self.write_snapshot("replica-head.json", self.tables(base))
        tampered = json.loads(base.read_text(encoding="utf-8"))
        tampered["digest"] = "0" * 64
        bad_base = self.scratch / "tampered-base.json"
        bad_base.write_text(json.dumps(tampered), encoding="utf-8")
        self.assertIn(
            "digest mismatch",
            self.failed(
                "sync-bundle", "--replica-ref", "test-replica", "--base", str(bad_base),
                "--head", str(replica), "--output", str(self.scratch / "bad-bundle.json"),
            )["error"],
        )

        forged_tables = self.tables(base)
        forged_tables["locations"].append(
            {
                "location_id": "loc-forged-base",
                "name": "Forged base",
                "kind": "room",
                "parent_location_id": None,
                "sensitivity": "low",
                "notes": None,
            }
        )
        forged_base = self.write_snapshot("forged-base.json", forged_tables)
        forged_head = self.write_snapshot("forged-head.json", forged_tables)
        forged_bundle = self.bundle(forged_base, forged_head, "forged-bundle.json")
        self.assertEqual(stat.S_IMODE(forged_bundle.stat().st_mode), 0o600)
        plans = self.runtime / "sync-plans"
        before = sorted(plans.iterdir()) if plans.exists() else []
        self.assertIn(
            "stale",
            self.failed(
                "sync-prepare", "--bundle", str(forged_bundle),
                "--trusted-base", str(base),
            )["error"],
        )
        self.assertEqual(before, sorted(plans.iterdir()) if plans.exists() else [])

    def test_replica_cannot_directly_rewrite_historical_or_projected_rows(self) -> None:
        """A verifier-valid snapshot still needs a transaction-backed delta."""
        self.cli(
            "add-location", "--location-id", "loc-proof", "--name", "Proof room",
            "--kind", "room", "--sensitivity", "low",
        )
        discovered = self.cli(
            "discover",
            "--actor", "sync integration test",
            "--source-ref", "physical check fixture",
            "--name", "Replica-proof item",
            "--category", "test fixture",
            "--checked-on", "2026-08-06",
            "--location-id", "loc-proof",
            "--new-model",
            "--new-unit",
            "--quantity", "1",
            "--sensitivity", "low",
        )["result"]
        base = self.snapshot("provenance-base.json")
        before = {
            path.relative_to(self.store).as_posix(): path.read_bytes()
            for path in self.store.glob("*.jsonl")
        }
        cases = {
            "model": (
                "models",
                lambda tables: next(
                    row for row in tables["models"]
                    if row["model_id"] == discovered["model_id"]
                ).update({"name": "Forged model name"}),
                "immutable models",
            ),
            "condition": (
                "items",
                lambda tables: next(
                    row for row in tables["items"]
                    if row["item_id"] == discovered["item_id"]
                ).update({"condition": "forged condition"}),
                "item detail change",
            ),
            "serial": (
                "items",
                lambda tables: next(
                    row for row in tables["items"]
                    if row["item_id"] == discovered["item_id"]
                ).update({"serial_or_lot": "forged serial"}),
                "item detail change",
            ),
            "evidence": (
                "evidence",
                lambda tables: next(
                    row for row in tables["evidence"]
                    if row["evidence_id"] == discovered["evidence_id"]
                ).update({"source_ref": "forged evidence"}),
                "immutable evidence",
            ),
        }
        for label, (_, mutate, expected_error) in cases.items():
            with self.subTest(label=label):
                replica_tables = self.tables(base)
                mutate(replica_tables)
                replica = self.write_snapshot(f"provenance-{label}.json", replica_tables)
                bundle = self.bundle(base, replica, f"provenance-{label}-bundle.json")
                rejected = self.failed(
                    "sync-prepare", "--bundle", str(bundle), "--trusted-base", str(base)
                )
                self.assertIn(expected_error, rejected["error"])
                after = {
                    path.relative_to(self.store).as_posix(): path.read_bytes()
                    for path in self.store.glob("*.jsonl")
                }
                self.assertEqual(after, before)

    def test_real_verified_export_can_be_the_retained_replica_base(self) -> None:
        archive = self.scratch / "base-export.tar.gz"
        self.cli("export", "--output", str(archive))
        head = self.snapshot("head.json")
        bundle = self.bundle(archive, head, "export-base-bundle.json")
        prepared = self.cli(
            "sync-prepare", "--bundle", str(bundle), "--trusted-base", str(archive)
        )
        self.assertEqual(prepared["status"], "ready")

    def test_existing_plan_checkout_round_trips_from_a_real_offline_replica(self) -> None:
        planned = self.cli(
            "plan", "--actor", "sync integration test", "--source-ref", "Cart base",
            "--name", "Offline checkout fixture", "--category", "test fixture",
            "--planned-on", "2026-08-05", "--quantity", "1", "--sensitivity", "low",
        )["result"]
        base = self.snapshot("checkout-base.json")
        archive = self.scratch / "checkout-base.tar.gz"
        self.cli("export", "--output", str(archive))

        replica_root = self.scratch / "replica-inventory"
        replica_runtime = self.scratch / "replica-runtime"
        replica_media = self.scratch / "replica-media"
        replica_catalogue = self.scratch / "replica-catalogue.md"

        def replica_cli(*arguments: str) -> dict:
            complete = subprocess.run(
                [
                    sys.executable, str(CLI), "--inventory-root", str(replica_root),
                    "--runtime-dir", str(replica_runtime), "--media-root", str(replica_media),
                    "--catalogue-output", str(replica_catalogue), "--scope", "private",
                    *arguments,
                ],
                text=True,
                capture_output=True,
                check=False,
            )
            self.assertEqual(complete.returncode, 0, complete.stderr or complete.stdout)
            return json.loads(complete.stdout)

        replica_cli("restore", "--archive", str(archive))
        ordered = replica_cli(
            "order", "--actor", "offline replica", "--source-ref", "Placed order",
            "--name", "Offline checkout fixture", "--category", "test fixture",
            "--ordered-on", "2026-08-06", "--order-placed",
            "--existing-item-id", planned["item_id"], "--quantity", "2",
            "--purchase-price", "24.50", "--purchase-currency", "GBP",
            "--receipt-ref", "offline-receipt",
        )
        self.assertIsNotNone(ordered["result"]["detail_amendment_id"])
        head = self.scratch / "checkout-head.json"
        replica_cli("sync-snapshot", "--output", str(head))
        bundle = self.bundle(base, head, "checkout-bundle.json")
        prepared = self.cli(
            "sync-prepare", "--bundle", str(bundle), "--trusted-base", str(base)
        )
        self.assertEqual(prepared["status"], "ready")
        self.cli("sync-apply", prepared["plan_id"])
        shown = self.cli("show", planned["item_id"])
        self.assertEqual(shown["item"]["ownership_state"], "candidate")
        self.assertEqual(shown["item"]["quantity"], 2)
        self.assertEqual(shown["item"]["purchase_price"], 24.5)
        self.assertEqual(shown["item"]["receipt_ref"], "offline-receipt")
        self.assertEqual(self.cli("status")["verification"]["failures"], [])

    def test_real_offline_physical_discovery_creates_a_replayable_item(self) -> None:
        self.cli(
            "add-location", "--location-id", "loc-offline-room",
            "--name", "Offline room", "--kind", "room", "--sensitivity", "low",
        )
        base = self.snapshot("creation-base.json")
        archive = self.scratch / "creation-base.tar.gz"
        self.cli("export", "--output", str(archive))
        replica_root = self.scratch / "creation-replica"
        replica_runtime = self.scratch / "creation-runtime"
        replica_media = self.scratch / "creation-media"
        replica_catalogue = self.scratch / "creation-catalogue.md"

        def replica_cli(*arguments: str) -> dict:
            complete = subprocess.run(
                [
                    sys.executable, str(CLI), "--inventory-root", str(replica_root),
                    "--runtime-dir", str(replica_runtime), "--media-root", str(replica_media),
                    "--catalogue-output", str(replica_catalogue), "--scope", "private",
                    *arguments,
                ],
                text=True, capture_output=True, check=False,
            )
            self.assertEqual(complete.returncode, 0, complete.stderr or complete.stdout)
            return json.loads(complete.stdout)

        replica_cli("restore", "--archive", str(archive))
        discovered = replica_cli(
            "discover", "--actor", "offline replica", "--source-ref", "overview photo",
            "--name", "Offline discovered fixture", "--category", "test fixture",
            "--checked-on", "2026-08-06", "--location-id", "loc-offline-room",
            "--quantity", "2", "--unit", "item", "--condition", "working",
            "--sensitivity", "low", "--new-unit",
        )["result"]
        head = self.scratch / "creation-head.json"
        replica_cli("sync-snapshot", "--output", str(head))
        prepared = self.cli(
            "sync-prepare",
            "--bundle", str(self.bundle(base, head, "creation-bundle.json")),
            "--trusted-base", str(base),
        )
        self.assertEqual(prepared["status"], "ready")
        self.cli("sync-apply", prepared["plan_id"])
        shown = self.cli("show", discovered["item_id"])
        self.assertEqual(shown["item"]["ownership_state"], "confirmed")
        self.assertEqual(shown["item"]["verified_on"], "2026-08-06")
        self.assertEqual(shown["item"]["quantity"], 2)
        self.assertEqual(self.cli("status")["verification"]["failures"], [])

    def test_real_offline_discovery_attached_image_syncs_in_a_bounded_sidecar(self) -> None:
        self.cli("add-location", "--location-id", "loc-media-room", "--name", "Media room", "--kind", "room", "--sensitivity", "low")
        base = self.snapshot("media-base.json")
        archive = self.scratch / "media-base.tar.gz"
        self.cli("export", "--output", str(archive))
        replica_root, replica_runtime = self.scratch / "media-replica", self.scratch / "media-replica-runtime"
        replica_media, replica_catalogue = self.scratch / "media-replica-bytes", self.scratch / "media-replica.md"

        def replica_cli(*arguments: str) -> dict:
            complete = subprocess.run([sys.executable, str(CLI), "--inventory-root", str(replica_root), "--runtime-dir", str(replica_runtime), "--media-root", str(replica_media), "--catalogue-output", str(replica_catalogue), "--scope", "private", *arguments], text=True, capture_output=True, check=False)
            self.assertEqual(complete.returncode, 0, complete.stderr or complete.stdout)
            return json.loads(complete.stdout)

        replica_cli("restore", "--archive", str(archive))
        discovered = replica_cli("discover", "--actor", "offline replica", "--source-ref", "image overview", "--name", "Offline image fixture", "--category", "test fixture", "--checked-on", "2026-08-06", "--location-id", "loc-media-room", "--quantity", "1", "--unit", "item", "--condition", "working", "--sensitivity", "low", "--new-unit")["result"]
        image = self.scratch / "offline-proof.png"
        Image.new("RGB", (4, 3), (20, 30, 40)).save(image, format="PNG")
        attached = replica_cli("attach-media", "--evidence-id", discovered["evidence_id"], "--file", str(image), "--role", "source", "--captured-on", "2026-08-06", "--media-type", "image/png", "--sensitivity", "low")["result"]
        head = self.scratch / "media-head.json"
        replica_cli("sync-snapshot", "--output", str(head))
        bundle = self.scratch / "media-bundle.json"
        replica_cli("sync-bundle", "--replica-ref", "test-replica", "--base", str(base), "--head", str(head), "--output", str(bundle))
        sidecar = bundle.with_name(f"{bundle.name}.media")
        self.assertTrue(sidecar.joinpath("sha256", attached["sha256"][:2], attached["sha256"]).is_file())
        expected_media = sidecar / "sha256" / attached["sha256"][:2] / attached["sha256"]

        def copied_sidecar(name: str) -> tuple[Path, Path]:
            candidate = self.scratch / name
            shutil.copyfile(bundle, candidate)
            candidate_sidecar = candidate.with_name(f"{candidate.name}.media")
            shutil.copytree(sidecar, candidate_sidecar)
            return candidate, candidate_sidecar

        tampered_bundle, tampered_sidecar = copied_sidecar("tampered-media-bundle.json")
        tampered_sidecar.joinpath("sha256", attached["sha256"][:2], attached["sha256"]).write_bytes(b"tampered")
        self.assertIn("invalid size", self.failed("sync-prepare", "--bundle", str(tampered_bundle), "--trusted-base", str(base))["error"])
        extra_bundle, extra_sidecar = copied_sidecar("extra-media-bundle.json")
        extra_sidecar.joinpath("extra").write_bytes(b"no")
        self.assertIn("missing or extra", self.failed("sync-prepare", "--bundle", str(extra_bundle), "--trusted-base", str(base))["error"])
        link_bundle, link_sidecar = copied_sidecar("symlink-media-bundle.json")
        linked = link_sidecar / "sha256" / attached["sha256"][:2] / attached["sha256"]
        linked.unlink()
        linked.symlink_to(expected_media)
        self.assertIn("symlink", self.failed("sync-prepare", "--bundle", str(link_bundle), "--trusted-base", str(base))["error"])
        prepared = self.cli("sync-prepare", "--bundle", str(bundle), "--trusted-base", str(base))
        self.assertEqual(prepared["status"], "ready")
        self.cli("sync-apply", prepared["plan_id"])
        self.assertTrue(self.media.joinpath("sha256", attached["sha256"][:2], attached["sha256"]).is_file())
        self.assertEqual(self.cli("status")["verification"]["failures"], [])

    def test_capture_transaction_conflict_requires_rebase_before_resolution(self) -> None:
        self.cli("add-location", "--location-id", "loc-capture-sync", "--name", "Capture shelf", "--kind", "room", "--sensitivity", "low")
        self.cli("add-location", "--location-id", "loc-capture-moved", "--name", "Moved shelf", "--kind", "room", "--sensitivity", "low")
        item_id = self.cli("discover", "--actor", "sync capture", "--source-ref", "physical base", "--name", "Capture sync item", "--category", "fixture", "--checked-on", "2026-08-05", "--location-id", "loc-capture-sync", "--quantity", "1", "--unit", "item", "--sensitivity", "low", "--new-unit")["result"]["item_id"]
        base = self.snapshot("capture-sync-base.json")
        archive = self.scratch / "capture-sync-base.tar.gz"
        self.cli("export", "--output", str(archive))
        replica_root, replica_runtime = self.scratch / "capture-sync-replica", self.scratch / "capture-sync-runtime"
        replica_media, replica_catalogue = self.scratch / "capture-sync-media", self.scratch / "capture-sync-catalogue.md"

        def replica_cli(*arguments: str) -> dict:
            complete = subprocess.run([sys.executable, str(CLI), "--inventory-root", str(replica_root), "--runtime-dir", str(replica_runtime), "--media-root", str(replica_media), "--catalogue-output", str(replica_catalogue), "--scope", "private", *arguments], text=True, capture_output=True, check=False)
            self.assertEqual(complete.returncode, 0, complete.stderr or complete.stdout)
            return json.loads(complete.stdout)

        replica_cli("restore", "--archive", str(archive))
        overview = self.scratch / "capture-sync-overview.png"
        Image.new("RGB", (12, 9), (21, 31, 41)).save(overview, format="PNG")
        staged = replica_cli("capture-prepare", "--overview", str(overview), "--captured-on", "2026-08-06", "--segments", '[{"segment_id":"label","region":{"x":2,"y":1,"width":5,"height":4}}]', "--source-ref", "offline capture")
        capture = staged["capture"]
        artifact = json.loads((replica_runtime / "capture-staging" / capture["capture_session_id"] / "artifact.json").read_text())
        crop = artifact["crops"][0]
        decision = [{"crop_id": crop["crop_id"], "segment_id": "label", "observation_id": None, "item_id": item_id, "discovery": None, "physical": {"actor": "offline capture", "checked_on": "2026-08-06", "condition": "used", "container_id": None, "location_id": "loc-capture-sync", "notes": None, "quantity": 2, "serial_or_lot": None, "unit": "item"}}]
        reviewed = replica_cli("capture-review", capture["capture_session_id"], "--artifact-sha256", capture["artifact_sha256"], "--links", "{}", "--decisions", json.dumps(decision))
        replica_cli("proposal-apply", reviewed["proposal"]["proposal_id"])
        head = self.scratch / "capture-sync-head.json"
        replica_cli("sync-snapshot", "--output", str(head))
        bundle = self.scratch / "capture-sync-bundle.json"
        replica_cli("sync-bundle", "--replica-ref", "capture-replica", "--base", str(base), "--head", str(head), "--output", str(bundle))
        self.cli("move", "--actor", "canonical", "--source-ref", "move", "--item-id", item_id, "--moved-on", "2026-08-06", "--location-id", "loc-capture-moved")
        prepared = self.cli("sync-prepare", "--bundle", str(bundle), "--trusted-base", str(base))
        conflict = next(row for row in prepared["plan"]["conflicts"] if row["table"] == "items" and row["identity"] == [item_id])
        self.assertEqual(conflict["kind"], "identity_collision_requires_rebase")
        self.assertEqual(conflict["choices"], [])
        self.assertTrue(
            any(row["table"] in {"capture_sessions", "capture_observations"} for row in conflict["dependent_replica_rows"]),
            conflict["dependent_replica_rows"],
        )

    def test_not_found_event_against_canonical_move_requires_rebase(self) -> None:
        for location_id in ("loc-event-a", "loc-event-b"):
            self.cli(
                "add-location", "--location-id", location_id, "--name", location_id,
                "--kind", "room", "--sensitivity", "low",
            )
        item_id = self.cli(
            "discover", "--actor", "event sync", "--source-ref", "base",
            "--name", "Event item", "--category", "fixture", "--checked-on", "2026-08-05",
            "--location-id", "loc-event-a", "--quantity", "1", "--unit", "item",
            "--sensitivity", "low", "--new-unit",
        )["result"]["item_id"]
        base = self.snapshot("event-base.json")
        archive = self.scratch / "event-base.tar.gz"
        self.cli("export", "--output", str(archive))
        replica_root = self.scratch / "event-replica"
        replica_runtime = self.scratch / "event-runtime"
        replica_media = self.scratch / "event-media"

        def replica_cli(*arguments: str) -> dict:
            complete = subprocess.run(
                [
                    sys.executable, str(CLI), "--inventory-root", str(replica_root),
                    "--runtime-dir", str(replica_runtime), "--media-root", str(replica_media),
                    "--catalogue-output", str(self.scratch / "event.md"), "--scope", "private",
                    *arguments,
                ],
                text=True, capture_output=True, check=False,
            )
            self.assertEqual(complete.returncode, 0, complete.stderr or complete.stdout)
            return json.loads(complete.stdout)

        replica_cli("restore", "--archive", str(archive))
        replica_cli(
            "not-found", "--actor", "offline", "--source-ref", "checked b",
            "--item-id", item_id, "--area-location-id", "loc-event-b",
            "--checked-on", "2026-08-06",
        )
        head = self.scratch / "event-head.json"
        replica_cli("sync-snapshot", "--output", str(head))
        bundle = self.scratch / "event-bundle.json"
        replica_cli(
            "sync-bundle", "--replica-ref", "event", "--base", str(base),
            "--head", str(head), "--output", str(bundle),
        )
        self.cli(
            "move", "--actor", "canonical", "--source-ref", "move b",
            "--item-id", item_id, "--moved-on", "2026-08-06", "--location-id", "loc-event-b",
        )
        prepared = self.cli("sync-prepare", "--bundle", str(bundle), "--trusted-base", str(base))
        self.assertEqual(prepared["status"], "needs_resolution")
        conflict = next(row for row in prepared["plan"]["conflicts"] if row["table"] == "inventory_events")
        self.assertEqual((conflict["kind"], conflict["choices"]), ("identity_collision_requires_rebase", []))

    def test_event_commutes_when_canonical_changes_a_different_item(self) -> None:
        for location_id in ("loc-commute-a", "loc-commute-b"):
            self.cli(
                "add-location", "--location-id", location_id, "--name", location_id,
                "--kind", "room", "--sensitivity", "low",
            )
        first = self.cli(
            "discover", "--actor", "event sync", "--source-ref", "first",
            "--name", "First", "--category", "fixture", "--checked-on", "2026-08-05",
            "--location-id", "loc-commute-a", "--quantity", "1", "--unit", "item",
            "--sensitivity", "low", "--new-unit",
        )["result"]["item_id"]
        second = self.cli(
            "discover", "--actor", "event sync", "--source-ref", "second",
            "--name", "Second", "--category", "fixture", "--checked-on", "2026-08-05",
            "--location-id", "loc-commute-a", "--quantity", "1", "--unit", "item",
            "--sensitivity", "low", "--new-unit",
        )["result"]["item_id"]
        base = self.snapshot("commute-base.json")
        archive = self.scratch / "commute-base.tar.gz"
        self.cli("export", "--output", str(archive))
        replica_root = self.scratch / "commute-replica"
        replica_runtime = self.scratch / "commute-runtime"
        replica_media = self.scratch / "commute-media"

        def replica_cli(*arguments: str) -> dict:
            complete = subprocess.run(
                [
                    sys.executable, str(CLI), "--inventory-root", str(replica_root),
                    "--runtime-dir", str(replica_runtime), "--media-root", str(replica_media),
                    "--catalogue-output", str(self.scratch / "commute.md"), "--scope", "private",
                    *arguments,
                ],
                text=True, capture_output=True, check=False,
            )
            self.assertEqual(complete.returncode, 0, complete.stderr or complete.stdout)
            return json.loads(complete.stdout)

        replica_cli("restore", "--archive", str(archive))
        replica_cli(
            "physical-check", "--actor", "offline", "--source-ref", "check",
            "--item-id", first, "--checked-on", "2026-08-06", "--location-unchanged",
        )
        head = self.scratch / "commute-head.json"
        replica_cli("sync-snapshot", "--output", str(head))
        bundle = self.scratch / "commute-bundle.json"
        replica_cli(
            "sync-bundle", "--replica-ref", "commute", "--base", str(base),
            "--head", str(head), "--output", str(bundle),
        )
        self.cli(
            "move", "--actor", "canonical", "--source-ref", "move",
            "--item-id", second, "--moved-on", "2026-08-06", "--location-id", "loc-commute-b",
        )
        prepared = self.cli("sync-prepare", "--bundle", str(bundle), "--trusted-base", str(base))
        self.assertEqual(prepared["status"], "ready")
        self.cli("sync-apply", prepared["plan_id"])
        self.assertEqual(self.cli("status")["verification"]["failures"], [])

    def test_event_against_canonical_aba_history_requires_rebase(self) -> None:
        for location_id in ("loc-aba-a", "loc-aba-b"):
            self.cli(
                "add-location", "--location-id", location_id, "--name", location_id,
                "--kind", "room", "--sensitivity", "low",
            )
        item_id = self.cli(
            "discover", "--actor", "event sync", "--source-ref", "base",
            "--name", "ABA event item", "--category", "fixture", "--checked-on", "2026-08-05",
            "--location-id", "loc-aba-a", "--quantity", "1", "--unit", "item",
            "--sensitivity", "low", "--new-unit",
        )["result"]["item_id"]
        base = self.snapshot("aba-event-base.json")
        archive = self.scratch / "aba-event-base.tar.gz"
        self.cli("export", "--output", str(archive))
        replica_root = self.scratch / "aba-event-replica"
        replica_runtime = self.scratch / "aba-event-runtime"
        replica_media = self.scratch / "aba-event-media"

        def replica_cli(*arguments: str) -> dict:
            complete = subprocess.run(
                [
                    sys.executable, str(CLI), "--inventory-root", str(replica_root),
                    "--runtime-dir", str(replica_runtime), "--media-root", str(replica_media),
                    "--catalogue-output", str(self.scratch / "aba-event.md"), "--scope", "private",
                    *arguments,
                ],
                text=True, capture_output=True, check=False,
            )
            self.assertEqual(complete.returncode, 0, complete.stderr or complete.stdout)
            return json.loads(complete.stdout)

        replica_cli("restore", "--archive", str(archive))
        replica_cli(
            "not-found", "--actor", "offline", "--source-ref", "checked b",
            "--item-id", item_id, "--area-location-id", "loc-aba-b",
            "--checked-on", "2026-08-06",
        )
        head = self.scratch / "aba-event-head.json"
        replica_cli("sync-snapshot", "--output", str(head))
        bundle = self.scratch / "aba-event-bundle.json"
        replica_cli(
            "sync-bundle", "--replica-ref", "aba-event", "--base", str(base),
            "--head", str(head), "--output", str(bundle),
        )
        self.cli(
            "move", "--actor", "canonical", "--source-ref", "move to b",
            "--item-id", item_id, "--moved-on", "2026-08-07", "--location-id", "loc-aba-b",
        )
        self.cli(
            "move", "--actor", "canonical", "--source-ref", "move to a",
            "--item-id", item_id, "--moved-on", "2026-08-08", "--location-id", "loc-aba-a",
        )
        prepared = self.cli("sync-prepare", "--bundle", str(bundle), "--trusted-base", str(base))
        self.assertEqual(prepared["status"], "needs_resolution")
        conflict = next(
            row for row in prepared["plan"]["conflicts"]
            if row["table"] == "inventory_events"
        )
        self.assertEqual((conflict["kind"], conflict["choices"]), ("identity_collision_requires_rebase", []))

    def test_replica_only_identity_model_conflict_requires_rebase(self) -> None:
        self.cli("add-location", "--location-id", "loc-model-sync", "--name", "Model shelf", "--kind", "room", "--sensitivity", "low")
        self.cli("add-location", "--location-id", "loc-model-moved", "--name", "Moved shelf", "--kind", "room", "--sensitivity", "low")
        item_id = self.cli("discover", "--actor", "sync model", "--source-ref", "base physical", "--name", "Base model item", "--category", "fixture", "--checked-on", "2026-08-05", "--location-id", "loc-model-sync", "--quantity", "1", "--unit", "item", "--sensitivity", "low", "--new-unit")["result"]["item_id"]
        base = self.snapshot("model-sync-base.json")
        archive = self.scratch / "model-sync-base.tar.gz"
        self.cli("export", "--output", str(archive))
        replica_root, replica_runtime = self.scratch / "model-sync-replica", self.scratch / "model-sync-runtime"
        replica_media, replica_catalogue = self.scratch / "model-sync-media", self.scratch / "model-sync-catalogue.md"

        def replica_cli(*arguments: str) -> dict:
            complete = subprocess.run([sys.executable, str(CLI), "--inventory-root", str(replica_root), "--runtime-dir", str(replica_runtime), "--media-root", str(replica_media), "--catalogue-output", str(replica_catalogue), "--scope", "private", *arguments], text=True, capture_output=True, check=False)
            self.assertEqual(complete.returncode, 0, complete.stderr or complete.stdout)
            return json.loads(complete.stdout)

        replica_cli("restore", "--archive", str(archive))
        evidence_id = replica_cli("record-evidence", "--item-id", item_id, "--source-ref", "corrected label", "--captured-on", "2026-08-06", "--evidence-type", "user_source", "--claim-strength", "claimed_owned", "--sensitivity", "low")["result"]["evidence_id"]
        replica_cli("correct-item-identity", "--actor", "offline", "--item-id", item_id, "--evidence-id", evidence_id, "--amended-on", "2026-08-06", "--reason", "identity_correction", "--name", "Replica-only corrected model", "--category", "fixture", "--new-model")
        head = self.scratch / "model-sync-head.json"
        replica_cli("sync-snapshot", "--output", str(head))
        bundle = self.scratch / "model-sync-bundle.json"
        replica_cli("sync-bundle", "--replica-ref", "model-replica", "--base", str(base), "--head", str(head), "--output", str(bundle))
        self.cli("move", "--actor", "canonical", "--source-ref", "move", "--item-id", item_id, "--moved-on", "2026-08-06", "--location-id", "loc-model-moved")
        prepared = self.cli("sync-prepare", "--bundle", str(bundle), "--trusted-base", str(base))
        conflict = next(row for row in prepared["plan"]["conflicts"] if row["table"] == "items" and row["identity"] == [item_id])
        self.assertEqual(conflict["choices"], [])
        self.assertTrue(any(row["table"] == "models" for row in conflict["dependent_replica_rows"]))

    def test_item_conflict_resolution_preserves_canonical_lifecycle_history(self) -> None:
        for location_id, name in (("loc-a", "Room A"), ("loc-b", "Room B")):
            self.cli(
                "add-location", "--location-id", location_id, "--name", name,
                "--kind", "room", "--sensitivity", "low",
            )
        item_id = self.cli(
            "discover", "--actor", "sync integration test", "--source-ref", "base check",
            "--name", "Lifecycle conflict fixture", "--category", "test fixture",
            "--checked-on", "2026-08-04", "--location-id", "loc-a",
            "--quantity", "1", "--unit", "item", "--sensitivity", "low", "--new-unit",
        )["result"]["item_id"]
        base = self.snapshot("lifecycle-base.json")
        archive = self.scratch / "lifecycle-base.tar.gz"
        self.cli("export", "--output", str(archive))
        replica_root = self.scratch / "lifecycle-replica"
        replica_runtime = self.scratch / "lifecycle-runtime"
        replica_media = self.scratch / "lifecycle-media"
        replica_catalogue = self.scratch / "lifecycle-catalogue.md"

        def replica_cli(*arguments: str) -> dict:
            complete = subprocess.run(
                [
                    sys.executable, str(CLI), "--inventory-root", str(replica_root),
                    "--runtime-dir", str(replica_runtime), "--media-root", str(replica_media),
                    "--catalogue-output", str(replica_catalogue), "--scope", "private",
                    *arguments,
                ],
                text=True, capture_output=True, check=False,
            )
            self.assertEqual(complete.returncode, 0, complete.stderr or complete.stdout)
            return json.loads(complete.stdout)

        replica_cli("restore", "--archive", str(archive))
        checked = replica_cli(
            "physical-check", "--actor", "offline replica", "--source-ref", "counted two",
            "--item-id", item_id, "--checked-on", "2026-08-05", "--location-unchanged",
            "--quantity", "2",
        )["result"]
        replica_photo = self.scratch / "rejected-replica-photo.png"
        Image.new("RGB", (4, 3), (80, 70, 60)).save(replica_photo, format="PNG")
        attached = replica_cli(
            "attach-media", "--evidence-id", checked["evidence_id"], "--file", str(replica_photo),
            "--role", "source", "--captured-on", "2026-08-05", "--media-type", "image/png",
            "--sensitivity", "low",
        )["result"]
        unrelated_evidence = replica_cli(
            "record-evidence", "--source-ref", "unrelated research", "--captured-on", "2026-08-05",
            "--evidence-type", "user_source", "--claim-strength", "research_only", "--sensitivity", "low",
        )["result"]["evidence_id"]
        unrelated_photo = self.scratch / "unrelated-replica-photo.png"
        Image.new("RGB", (4, 3), (20, 70, 30)).save(unrelated_photo, format="PNG")
        unrelated_asset = replica_cli(
            "attach-media", "--evidence-id", unrelated_evidence, "--file", str(unrelated_photo),
            "--role", "source", "--captured-on", "2026-08-05", "--media-type", "image/png", "--sensitivity", "low",
        )["result"]
        support_evidence = replica_cli(
            "record-evidence", "--item-id", item_id, "--source-ref", "independent item support",
            "--captured-on", "2026-08-05", "--evidence-type", "user_source",
            "--claim-strength", "claimed_owned", "--sensitivity", "low",
        )["result"]["evidence_id"]
        support_photo = self.scratch / "support-replica-photo.png"
        Image.new("RGB", (4, 3), (30, 40, 80)).save(support_photo, format="PNG")
        support_asset = replica_cli(
            "attach-media", "--evidence-id", support_evidence, "--file", str(support_photo),
            "--role", "source", "--captured-on", "2026-08-05", "--media-type", "image/png", "--sensitivity", "low",
        )["result"]
        replica_head = self.scratch / "lifecycle-head.json"
        replica_cli("sync-snapshot", "--output", str(replica_head))
        self.cli(
            "move", "--actor", "canonical writer", "--source-ref", "moved to room B",
            "--item-id", item_id, "--moved-on", "2026-08-05", "--location-id", "loc-b",
        )
        bundle = self.scratch / "lifecycle-bundle.json"
        replica_cli(
            "sync-bundle", "--replica-ref", "test-replica", "--base", str(base),
            "--head", str(replica_head), "--output", str(bundle),
        )
        canonical_plan = self.cli(
            "sync-prepare", "--bundle", str(bundle), "--trusted-base", str(base)
        )
        self.assertEqual(canonical_plan["status"], "needs_resolution")
        conflict = next(
            row for row in canonical_plan["plan"]["conflicts"]
            if row["table"] == "items" and row["identity"] == [item_id]
        )
        canonical_choices = self.scratch / "choose-canonical.json"
        replica_choices = self.scratch / "choose-replica.json"
        canonical_choices.write_text(
            json.dumps({conflict["conflict_id"]: "canonical"}), encoding="utf-8"
        )
        replica_choices.write_text(json.dumps({conflict["conflict_id"]: "replica"}), encoding="utf-8")
        self.assertIn(
            "invalid resolution",
            self.failed(
                "sync-resolve", canonical_plan["plan_id"],
                "--resolutions", str(replica_choices),
            )["error"],
        )
        canonical_ready = self.cli(
            "sync-resolve", canonical_plan["plan_id"],
            "--resolutions", str(canonical_choices),
        )
        self.assertEqual(canonical_ready["status"], "ready")
        canonical_types = {
            row["event_type"]
            for row in canonical_ready["plan"]["tables"]["inventory_events"]
            if row["item_id"] == item_id
        }
        self.assertIn("moved", canonical_types)
        self.assertFalse(
            any(
                row["evidence_id"] == checked["evidence_id"]
                for row in canonical_ready["plan"]["tables"]["inventory_events"]
            )
        )
        for evidence_id, asset in ((unrelated_evidence, unrelated_asset), (support_evidence, support_asset)):
            self.assertTrue(any(row["evidence_id"] == evidence_id for row in canonical_ready["plan"]["tables"]["evidence"]))
            self.assertTrue(any(row["evidence_id"] == evidence_id for row in canonical_ready["plan"]["tables"]["evidence_assets"]))
            self.assertTrue(any(row["sha256"] == asset["sha256"] for row in canonical_ready["plan"]["tables"]["media_assets"]))
        self.assertFalse(
            any(
                row["sha256"] == attached["sha256"]
                for row in canonical_ready["plan"]["tables"]["media_assets"]
            )
        )
        self.cli("sync-apply", canonical_plan["plan_id"])
        shown = self.cli("show", item_id)
        self.assertEqual(shown["item"]["location_id"], "loc-b")
        self.assertEqual(shown["item"]["quantity"], 1)
        self.assertFalse(
            any(
                row["sha256"] == attached["sha256"]
                for row in [json.loads(line) for line in self.store.joinpath("media_assets.jsonl").read_text().splitlines()]
            )
        )
        self.assertFalse(self.media.joinpath("sha256", attached["sha256"][:2], attached["sha256"]).exists())
        for asset in (unrelated_asset, support_asset):
            self.assertTrue(self.media.joinpath("sha256", asset["sha256"][:2], asset["sha256"]).is_file())
        self.assertEqual(self.cli("status")["verification"]["failures"], [])

    def test_item_conflict_resolution_preserves_canonical_detail_history(self) -> None:
        self.cli(
            "add-location", "--location-id", "loc-detail",
            "--name", "Detail room", "--kind", "room", "--sensitivity", "low",
        )
        item_id = self.cli(
            "discover", "--actor", "sync integration test", "--source-ref", "base check",
            "--name", "Detail conflict fixture", "--category", "test fixture",
            "--checked-on", "2026-08-04", "--location-id", "loc-detail",
            "--sensitivity", "low", "--new-unit",
        )["result"]["item_id"]
        base = self.snapshot("detail-base.json")
        archive = self.scratch / "detail-base.tar.gz"
        self.cli("export", "--output", str(archive))
        replica_root = self.scratch / "detail-replica"
        replica_runtime = self.scratch / "detail-runtime"
        replica_media = self.scratch / "detail-media"
        replica_catalogue = self.scratch / "detail-catalogue.md"

        def replica_cli(*arguments: str) -> dict:
            complete = subprocess.run(
                [
                    sys.executable, str(CLI), "--inventory-root", str(replica_root),
                    "--runtime-dir", str(replica_runtime), "--media-root", str(replica_media),
                    "--catalogue-output", str(replica_catalogue), "--scope", "private",
                    *arguments,
                ],
                text=True, capture_output=True, check=False,
            )
            self.assertEqual(complete.returncode, 0, complete.stderr or complete.stdout)
            return json.loads(complete.stdout)

        replica_cli("restore", "--archive", str(archive))
        replica_evidence = replica_cli(
            "record-evidence", "--item-id", item_id, "--source-ref", "condition check",
            "--captured-on", "2026-08-05", "--evidence-type", "user_source",
            "--claim-strength", "claimed_owned", "--sensitivity", "low",
        )["result"]["evidence_id"]
        replica_cli(
            "enrich-item", "--actor", "offline replica", "--item-id", item_id,
            "--evidence-id", replica_evidence, "--amended-on", "2026-08-05",
            "--condition", "working",
        )
        replica_head = self.scratch / "detail-head.json"
        replica_cli("sync-snapshot", "--output", str(replica_head))
        canonical_evidence = self.cli(
            "record-evidence", "--item-id", item_id, "--source-ref", "serial label",
            "--captured-on", "2026-08-05", "--evidence-type", "user_source",
            "--claim-strength", "claimed_owned", "--sensitivity", "low",
        )["result"]["evidence_id"]
        self.cli(
            "enrich-item", "--actor", "canonical writer", "--item-id", item_id,
            "--evidence-id", canonical_evidence, "--amended-on", "2026-08-05",
            "--serial-or-lot", "SERIAL-CANONICAL",
        )
        bundle = self.bundle(base, replica_head, "detail-bundle.json")
        first = self.cli(
            "sync-prepare", "--bundle", str(bundle), "--trusted-base", str(base)
        )
        conflict = next(
            row for row in first["plan"]["conflicts"]
            if row["table"] == "items" and row["identity"] == [item_id]
        )
        canonical_choices = self.scratch / "detail-canonical.json"
        replica_choices = self.scratch / "detail-replica.json"
        canonical_choices.write_text(
            json.dumps({conflict["conflict_id"]: "canonical"}), encoding="utf-8"
        )
        replica_choices.write_text(
            json.dumps({conflict["conflict_id"]: "replica"}), encoding="utf-8"
        )
        self.assertIn(
            "invalid resolution",
            self.failed(
                "sync-resolve", first["plan_id"], "--resolutions", str(replica_choices)
            )["error"],
        )
        canonical_ready = self.cli(
            "sync-resolve", first["plan_id"], "--resolutions", str(canonical_choices)
        )
        self.assertEqual(canonical_ready["status"], "ready")
        canonical_history = [
            row for row in canonical_ready["plan"]["tables"]["item_detail_amendments"]
            if row["item_id"] == item_id
        ]
        self.assertEqual(len(canonical_history), 1)
        self.assertIn("serial_or_lot", json.loads(canonical_history[0]["changes_json"]))
        self.cli("sync-apply", first["plan_id"])
        shown = self.cli("show", item_id)
        self.assertEqual(shown["item"]["serial_or_lot"], "SERIAL-CANONICAL")
        self.assertIsNone(shown["item"]["condition"])
        self.assertEqual(self.cli("status")["verification"]["failures"], [])

    def test_explicit_resolution_and_crash_retry_do_not_duplicate_receipts(self) -> None:
        self.cli("add-location", "--location-id", "loc-conflict", "--name", "Original", "--kind", "room", "--sensitivity", "low")
        base = self.snapshot("base.json")
        replica = self.replica_location_note_branch(
            self.tables(base), "loc-conflict", "replica edit"
        )
        self.amend_location_notes("loc-conflict", "canonical edit")
        bundle = self.bundle(base, self.write_snapshot("replica-head.json", replica), "bundle.json")
        conflict = self.cli(
            "sync-prepare", "--bundle", str(bundle), "--trusted-base", str(base)
        )
        self.assertEqual(conflict["status"], "needs_resolution")
        conflict_id = conflict["plan"]["conflicts"][0]["conflict_id"]
        resolutions = self.scratch / "resolutions.json"
        resolutions.write_text(json.dumps({conflict_id: "canonical"}), encoding="utf-8")
        resolved = self.cli("sync-resolve", conflict["plan_id"], "--resolutions", str(resolutions))
        self.assertEqual(resolved["status"], "ready")
        environment = dict(os.environ)
        environment["PROPERTY_INVENTORY_FAIL_SYNC_AFTER_COMMIT"] = "1"
        crashed = subprocess.run(
            self.command("sync-apply", conflict["plan_id"]), text=True, capture_output=True,
            check=False, env=environment,
        )
        self.assertEqual(crashed.returncode, 97)
        recovered = self.cli("sync-apply", conflict["plan_id"])
        self.assertEqual(recovered["status"], "recovered_applied")
        receipts = self.store.joinpath("sync_receipts.jsonl").read_text().splitlines()
        self.assertEqual(len(receipts), 1)
        location = next(
            json.loads(line)
            for line in self.store.joinpath("locations.jsonl").read_text().splitlines()
            if json.loads(line)["location_id"] == "loc-conflict"
        )
        self.assertEqual(location["notes"], "canonical edit")

    def test_rejected_fact_photo_is_pruned_before_canonical_apply(self) -> None:
        self.cli("add-location", "--location-id", "loc-photo-fact", "--name", "Original", "--kind", "room", "--sensitivity", "low")
        base = self.snapshot("fact-photo-base.json")
        archive = self.scratch / "fact-photo-base.tar.gz"
        self.cli("export", "--output", str(archive))
        replica_root, replica_runtime = self.scratch / "fact-photo-replica", self.scratch / "fact-photo-runtime"
        replica_media, replica_catalogue = self.scratch / "fact-photo-media", self.scratch / "fact-photo-catalogue.md"
        def replica_cli(*arguments: str) -> dict:
            complete = subprocess.run([sys.executable, str(CLI), "--inventory-root", str(replica_root), "--runtime-dir", str(replica_runtime), "--media-root", str(replica_media), "--catalogue-output", str(replica_catalogue), "--scope", "private", *arguments], text=True, capture_output=True, check=False)
            self.assertEqual(complete.returncode, 0, complete.stderr or complete.stdout)
            return json.loads(complete.stdout)
        replica_cli("restore", "--archive", str(archive))
        evidence_id = replica_cli("record-evidence", "--source-ref", "replica photo fact", "--captured-on", "2026-08-06", "--evidence-type", "user_source", "--claim-strength", "research_only", "--sensitivity", "low")["result"]["evidence_id"]
        photo = self.scratch / "fact-replica.png"
        Image.new("RGB", (4, 3), (2, 3, 4)).save(photo, format="PNG")
        attached = replica_cli("attach-media", "--evidence-id", evidence_id, "--file", str(photo), "--role", "source", "--captured-on", "2026-08-06", "--media-type", "image/png", "--sensitivity", "low")["result"]
        current = next(json.loads(line) for line in (replica_root / "Data" / "store" / "locations.jsonl").read_text().splitlines() if json.loads(line)["location_id"] == "loc-photo-fact")
        replica_cli("amend-fact", "--actor", "replica", "--table", "locations", "--selector", json.dumps({"location_id": "loc-photo-fact"}), "--action", "replace", "--replacement", json.dumps({**current, "notes": "replica"}), "--evidence-id", evidence_id, "--amended-on", "2026-08-06", "--reason", "fact photo")
        head = self.scratch / "fact-photo-head.json"
        replica_cli("sync-snapshot", "--output", str(head))
        bundle = self.scratch / "fact-photo-bundle.json"
        replica_cli("sync-bundle", "--replica-ref", "fact-photo", "--base", str(base), "--head", str(head), "--output", str(bundle))
        self.amend_location_notes("loc-photo-fact", "canonical")
        prepared = self.cli("sync-prepare", "--bundle", str(bundle), "--trusted-base", str(base))
        conflict_id = next(row["conflict_id"] for row in prepared["plan"]["conflicts"] if row["table"] == "locations")
        choices = self.scratch / "fact-photo-choice.json"
        choices.write_text(json.dumps({conflict_id: "canonical"}))
        ready = self.cli("sync-resolve", prepared["plan_id"], "--resolutions", str(choices))
        self.assertFalse(any(row.get("sha256") == attached["sha256"] for row in ready["plan"]["tables"]["media_assets"]))
        self.cli("sync-apply", prepared["plan_id"])
        self.assertFalse(self.media.joinpath("sha256", attached["sha256"][:2], attached["sha256"]).exists())
        self.assertEqual(self.cli("status")["verification"]["failures"], [])

    def test_noncanonical_resolution_is_rejected_without_erasing_history(self) -> None:
        self.cli("add-location", "--location-id", "loc-choice", "--name", "Original", "--kind", "room", "--sensitivity", "low")
        base = self.snapshot("choice-base.json")
        replica = self.replica_location_note_branch(
            self.tables(base), "loc-choice", "replica choice"
        )
        self.amend_location_notes("loc-choice", "canonical choice")
        bundle = self.bundle(
            base, self.write_snapshot("choice-head.json", replica), "choice-bundle.json"
        )
        first = self.cli(
            "sync-prepare", "--bundle", str(bundle), "--trusted-base", str(base)
        )
        conflict_id = first["plan"]["conflicts"][0]["conflict_id"]
        canonical_resolution = self.scratch / "canonical-resolution.json"
        replica_resolution = self.scratch / "replica-resolution.json"
        canonical_resolution.write_text(json.dumps({conflict_id: "canonical"}), encoding="utf-8")
        replica_resolution.write_text(json.dumps({conflict_id: "replica"}), encoding="utf-8")
        rejected = self.failed(
            "sync-resolve", first["plan_id"], "--resolutions", str(replica_resolution)
        )
        self.assertIn("invalid resolution", rejected["error"])
        still_pending = self.cli("sync-show", first["plan_id"])
        self.assertEqual(still_pending["plan"]["status"], "needs_resolution")
        self.cli("sync-resolve", first["plan_id"], "--resolutions", str(canonical_resolution))
        self.cli("sync-apply", first["plan_id"])
        location = next(
            json.loads(line)
            for line in self.store.joinpath("locations.jsonl").read_text().splitlines()
            if json.loads(line)["location_id"] == "loc-choice"
        )
        self.assertEqual(location["notes"], "canonical choice")

    def test_private_write_mcp_resolves_a_conflict_without_applying_it(self) -> None:
        self.cli(
            "add-location",
            "--location-id",
            "loc-mcp-conflict",
            "--name",
            "MCP conflict location",
            "--kind",
            "room",
            "--sensitivity",
            "low",
        )
        base = self.snapshot("mcp-conflict-base.json")
        replica = self.replica_location_note_branch(
            self.tables(base), "loc-mcp-conflict", "replica edit"
        )
        self.amend_location_notes("loc-mcp-conflict", "canonical edit")
        bundle = self.bundle(
            base,
            self.write_snapshot("mcp-conflict-replica.json", replica),
            "mcp-conflict-bundle.json",
        )
        before = {
            path.relative_to(self.store).as_posix(): path.read_bytes()
            for path in self.store.glob("*.jsonl")
        }

        async def scenario() -> str:
            async with Client(
                stdio_client(
                    StdioServerParameters(
                        command=str(MCP),
                        args=[
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
                            "--profile",
                            "write",
                        ],
                        cwd=self.scratch,
                    )
                ),
                mode="legacy",
            ) as client:
                prepared = await client.call_tool(
                    "prepare_replica_sync",
                    {"bundle_path": str(bundle), "trusted_base_path": str(base)},
                )
                self.assertFalse(prepared.is_error, prepared.content)
                self.assertEqual(prepared.structured_content["status"], "needs_resolution")
                conflict_id = prepared.structured_content["plan"]["conflicts"][0]["conflict_id"]
                resolved = await client.call_tool(
                    "resolve_replica_sync",
                    {
                        "plan_id": prepared.structured_content["plan_id"],
                        "resolutions": {conflict_id: "canonical"},
                    },
                )
                self.assertFalse(resolved.is_error, resolved.content)
                self.assertEqual(resolved.structured_content["status"], "ready")
                self.assertEqual(
                    resolved.structured_content["plan"]["resolutions"],
                    [{"conflict_id": conflict_id, "choice": "canonical"}],
                )
                verified = await client.call_tool("inventory_status", {})
                self.assertFalse(verified.is_error, verified.content)
                self.assertEqual(verified.structured_content["status"], "pass")
                return prepared.structured_content["plan_id"]

        plan_id = asyncio.run(scenario())
        after_resolution = {
            path.relative_to(self.store).as_posix(): path.read_bytes()
            for path in self.store.glob("*.jsonl")
        }
        self.assertEqual(after_resolution, before)
        applied = self.cli("sync-apply", plan_id)
        self.assertEqual(applied["status"], "committed_to_store")
        self.assertEqual(self.cli("status")["status"], "pass")
        location = next(
            json.loads(line)
            for line in self.store.joinpath("locations.jsonl").read_text().splitlines()
            if json.loads(line)["location_id"] == "loc-mcp-conflict"
        )
        self.assertEqual(location["notes"], "canonical edit")

    def test_private_scope_and_mcp_allowlist_never_expose_or_apply_sync_plans(self) -> None:
        base = self.snapshot("base.json")
        bundle = self.bundle(base, self.write_snapshot("replica-head.json", self.tables(base)), "bundle.json")
        self.assertEqual(
            self.failed(
                "sync-prepare", "--bundle", str(bundle), "--trusted-base", str(base),
                scope="personal",
            )["error"],
            "inventory command could not complete safely in this scope",
        )
        self.assertIn(
            "managed root",
            self.failed("sync-snapshot", "--output", str(self.runtime / "leak.json"))["error"],
        )
        symlinked_bundle = self.scratch / "bundle-link.json"
        symlinked_bundle.symlink_to(bundle)
        self.assertIn(
            "regular file",
            self.failed(
                "sync-prepare", "--bundle", str(symlinked_bundle),
                "--trusted-base", str(base),
            )["error"],
        )
        self.assertTrue(MCP.is_file(), f"MCP console script not installed: {MCP}")

        async def scenario() -> None:
            async with Client(
                stdio_client(
                    StdioServerParameters(
                        command=str(MCP),
                        args=[
                            "--inventory-root", str(self.root),
                            "--runtime-dir", str(self.runtime),
                            "--media-root", str(self.media),
                            "--catalogue-output", str(self.catalogue),
                            "--scope", "personal", "--profile", "read",
                        ],
                        cwd=self.scratch,
                    )
                ), mode="legacy",
            ) as client:
                names = {tool.name for tool in (await client.list_tools()).tools}
                self.assertNotIn("prepare_replica_sync", names)
                self.assertNotIn("inspect_replica_sync", names)
                self.assertNotIn("resolve_replica_sync", names)
            async with Client(
                stdio_client(
                    StdioServerParameters(
                        command=str(MCP),
                        args=[
                            "--inventory-root", str(self.root),
                            "--runtime-dir", str(self.runtime),
                            "--media-root", str(self.media),
                            "--catalogue-output", str(self.catalogue),
                            "--scope", "private", "--profile", "write",
                        ],
                        cwd=self.scratch,
                    )
                ), mode="legacy",
            ) as client:
                names = {tool.name for tool in (await client.list_tools()).tools}
                self.assertTrue({"prepare_replica_sync", "inspect_replica_sync", "resolve_replica_sync"} <= names)
                self.assertNotIn("apply_replica_sync", names)
                prepared = await client.call_tool(
                    "prepare_replica_sync",
                    {"bundle_path": str(bundle), "trusted_base_path": str(base)},
                )
                self.assertFalse(prepared.is_error, prepared.content)
                inspected = await client.call_tool("inspect_replica_sync", {"plan_id": prepared.structured_content["plan_id"]})
                self.assertFalse(inspected.is_error, inspected.content)

        asyncio.run(scenario())


if __name__ == "__main__":
    unittest.main()
