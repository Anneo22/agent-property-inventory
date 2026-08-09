"""End-to-end capture staging tests, including immutable proposal application."""

from __future__ import annotations

import asyncio
import hashlib
import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from mcp.client import Client
from mcp.client.stdio import StdioServerParameters, stdio_client
from PIL import Image

from property_inventory import cli as inventory_cli

HERE = Path(__file__).resolve().parents[1]
CLI = HERE / "property_inventory.py"
MCP = Path(sys.executable).parent / "property-inventory-mcp"
BENCHMARK = HERE / "tests" / "fixtures" / "capture" / "synthetic-executed-benchmark.json"


class CaptureIntegrationTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory(prefix="inventory-capture-integration-")
        self.scratch = Path(self.temp.name)
        self.root = self.scratch / "inventory"
        self.runtime = self.scratch / "runtime"
        self.media = self.scratch / "media"
        self.adapter_config = self.scratch / "capture-adapters.json"
        self.adapter_config.write_text(
            json.dumps(
                {
                    "version": 2,
                    "adapters": {
                        name: {"command": command, "revision": f"test:{name}:1"}
                        for name, command in {
                            "broken": ["/bin/false"],
                            "fixture": [
                                sys.executable,
                                str(
                                    HERE
                                    / "tests" / "fixtures"
                                    / "capture"
                                    / "valid_adapter.py"
                                ),
                            ],
                            "benchmark": [
                                sys.executable,
                                str(
                                    HERE
                                    / "tests" / "fixtures"
                                    / "capture"
                                    / "benchmark_adapter.py"
                                ),
                            ],
                            "orientation": [
                                sys.executable,
                                str(
                                    HERE
                                    / "tests" / "fixtures"
                                    / "capture"
                                    / "orientation_adapter.py"
                                ),
                            ],
                            "identifier": [
                                sys.executable,
                                str(
                                    HERE
                                    / "tests" / "fixtures"
                                    / "capture"
                                    / "identifier_adapter.py"
                                ),
                            ],
                            "nondeterministic": [
                                sys.executable,
                                str(
                                    HERE
                                    / "tests" / "fixtures"
                                    / "capture"
                                    / "nondeterministic_adapter.py"
                                ),
                            ],
                        }.items()
                    },
                }
            ),
            encoding="utf-8",
        )
        self.overview = self.scratch / "overview.png"
        Image.new("RGB", (12, 9), (20, 30, 40)).save(self.overview)
        self.cli("init")

    def tearDown(self) -> None:
        self.temp.cleanup()

    @property
    def store(self) -> Path:
        return self.root / "Data" / "store"

    def command(self, *arguments: str, scope: str = "private") -> list[str]:
        return [
            sys.executable, str(CLI), "--inventory-root", str(self.root),
            "--runtime-dir", str(self.runtime), "--media-root", str(self.media),
            "--capture-adapters-config", str(self.adapter_config),
            "--scope", scope, *arguments,
        ]

    def cli(self, *arguments: str, scope: str = "private") -> dict:
        complete = subprocess.run(self.command(*arguments, scope=scope), text=True, capture_output=True, check=False)
        self.assertEqual(complete.returncode, 0, complete.stderr or complete.stdout)
        return json.loads(complete.stdout)

    def failed(self, *arguments: str, scope: str = "private") -> dict:
        complete = subprocess.run(self.command(*arguments, scope=scope), text=True, capture_output=True, check=False)
        self.assertNotEqual(complete.returncode, 0, complete.stdout)
        return json.loads(complete.stderr)

    def snapshot(self) -> dict[str, bytes]:
        return {path.relative_to(self.store).as_posix(): path.read_bytes() for path in self.store.iterdir()}

    def prepare(self) -> dict:
        staged = self.cli(
            "capture-prepare", "--overview", str(self.overview), "--captured-on", "2026-08-06",
            "--segments", '[{"segment_id":"label","region":{"x":2,"y":1,"width":5,"height":4}}]',
            "--source-ref", "Integration overview photo",
        )
        reviewed = self.cli(
            "capture-review",
            staged["capture"]["capture_session_id"],
            "--artifact-sha256",
            staged["capture"]["artifact_sha256"],
            "--links",
            "{}",
        )
        reviewed["capture"].update(staged["capture"])
        return reviewed

    def test_real_overview_is_immutable_until_one_verified_proposal_apply(self) -> None:
        before = self.snapshot()
        prepared = self.prepare()
        proposal = prepared["proposal"]
        capture_id = prepared["capture"]["capture_session_id"]
        self.assertEqual(before, self.snapshot())
        staging = self.runtime / "capture-staging" / capture_id
        artifact = json.loads((staging / "artifact.json").read_text())
        self.assertEqual(artifact["segmentation_source"], "supplied")
        self.assertEqual(artifact["segments"][0]["segment_id"], "label")
        self.assertEqual(hashlib.sha256((staging / "overview").read_bytes()).hexdigest(), artifact["source"]["sha256"])
        self.assertEqual(len(artifact["crops"]), 1)
        applied = self.cli("proposal-apply", proposal["proposal_id"])
        self.assertEqual(applied["status"], "committed_to_store")
        self.assertEqual(applied["capture_staging_cleanup"], "removed")
        self.assertFalse(staging.exists())
        self.assertEqual(self.cli("capture-status", capture_id)["status"], "applied")
        self.assertEqual(len((self.store / "inventory_events.jsonl").read_text().splitlines()), 0)
        self.assertEqual(len((self.store / "capture_sessions.jsonl").read_text().splitlines()), 1)
        self.assertEqual(len([path for path in self.media.rglob("*") if path.is_file()]), 2)
        self.assertIn("not prepared", self.failed("proposal-apply", proposal["proposal_id"])["error"])

    def test_tampered_staging_and_untrusted_scope_are_rejected_without_store_write(self) -> None:
        prepared = self.prepare()
        capture_id = prepared["capture"]["capture_session_id"]
        artifact_path = self.runtime / "capture-staging" / capture_id / "artifact.json"
        artifact = json.loads(artifact_path.read_text(encoding="utf-8"))
        artifact["source"]["original_name"] = "tampered-but-valid.png"
        artifact_path.write_text(
            json.dumps(
                artifact,
                ensure_ascii=False,
                sort_keys=True,
                separators=(",", ":"),
            ),
            encoding="utf-8",
        )
        before = self.snapshot()
        status_failure = self.failed("capture-status", prepared["capture"]["capture_session_id"])
        self.assertIn("capture", status_failure["error"])
        failed = self.failed("proposal-apply", prepared["proposal"]["proposal_id"])
        self.assertIn("capture", failed["error"])
        self.assertEqual(before, self.snapshot())
        self.assertEqual(
            self.failed(
                "capture-prepare", "--overview", str(self.overview),
                "--captured-on", "2026-08-06", "--segments", "[]",
                "--source-ref", "x", scope="personal",
            )["error"],
            "inventory command could not complete safely in this scope",
        )

    def test_overview_byte_tampering_is_rejected_before_review_and_by_status(self) -> None:
        prepared = self.cli(
            "capture-prepare",
            "--overview",
            str(self.overview),
            "--captured-on",
            "2026-08-06",
            "--segments",
            '[{"segment_id":"label","region":{"x":2,"y":1,"width":5,"height":4}}]',
            "--source-ref",
            "Tampered overview",
        )
        capture = prepared["capture"]
        staging = self.runtime / "capture-staging" / capture["capture_session_id"]
        (staging / "overview").write_bytes(b"different overview bytes")
        before = self.snapshot()

        failed_review = self.failed(
            "capture-review",
            capture["capture_session_id"],
            "--artifact-sha256",
            capture["artifact_sha256"],
            "--links",
            "{}",
        )
        self.assertIn("disagree with the capture manifest", failed_review["error"])
        self.assertFalse((staging / "review.json").exists())
        failed_status = self.failed("capture-status", capture["capture_session_id"])
        self.assertIn("disagree with the capture manifest", failed_status["error"])
        self.assertEqual(before, self.snapshot())

    def test_reviewed_staging_rejects_every_unexpected_entry_without_store_write(self) -> None:
        for entry_kind in ("regular", "symlink", "fifo"):
            with self.subTest(entry_kind=entry_kind):
                prepared = self.prepare()
                capture_id = prepared["capture"]["capture_session_id"]
                staging = self.runtime / "capture-staging" / capture_id
                unexpected = staging / f"unexpected-{entry_kind}"
                if entry_kind == "regular":
                    unexpected.write_bytes(b"untrusted extra payload")
                elif entry_kind == "symlink":
                    unexpected.symlink_to(self.overview)
                else:
                    os.mkfifo(unexpected)
                before = self.snapshot()

                completed = subprocess.run(
                    self.command("capture-status", capture_id),
                    text=True,
                    capture_output=True,
                    check=False,
                    timeout=2,
                )
                self.assertNotEqual(completed.returncode, 0)
                self.assertIn(
                    "unexpected entries", json.loads(completed.stderr)["error"]
                )
                failed_apply = self.failed(
                    "proposal-apply", prepared["proposal"]["proposal_id"]
                )
                self.assertIn("unexpected entries", failed_apply["error"])
                self.assertEqual(before, self.snapshot())
                unexpected.unlink()

    def test_apply_rejects_a_consistent_staging_swap_after_proposal_validation(self) -> None:
        prepared = self.prepare()
        capture_id = prepared["capture"]["capture_session_id"]
        staging = self.runtime / "capture-staging" / capture_id
        original_copy = inventory_cli.copy_validated_capture_staging

        def swap_then_copy(source: Path, destination: Path, artifact: dict) -> None:
            artifact_path = source / "artifact.json"
            changed_artifact = json.loads(artifact_path.read_text(encoding="utf-8"))
            changed_artifact["source"]["original_name"] = "swapped-after-validation.png"
            artifact_path.write_text(
                json.dumps(
                    changed_artifact,
                    ensure_ascii=False,
                    sort_keys=True,
                    separators=(",", ":"),
                )
                + "\n",
                encoding="utf-8",
            )
            changed_artifact_digest = hashlib.sha256(
                artifact_path.read_bytes()
            ).hexdigest()
            review_path = source / "review.json"
            changed_review = json.loads(review_path.read_text(encoding="utf-8"))
            changed_review["artifact_sha256"] = changed_artifact_digest
            review_path.write_text(
                json.dumps(
                    changed_review,
                    ensure_ascii=False,
                    sort_keys=True,
                    separators=(",", ":"),
                )
                + "\n",
                encoding="utf-8",
            )
            original_copy(source, destination, artifact)

        before = self.snapshot()
        arguments = self.command(
            "proposal-apply", prepared["proposal"]["proposal_id"]
        )[2:]
        with patch.object(
            inventory_cli,
            "copy_validated_capture_staging",
            side_effect=swap_then_copy,
        ):
            with self.assertRaisesRegex(
                inventory_cli.InventoryError,
                "changed while proposal inputs were materialized",
            ):
                inventory_cli.execute(arguments)
        self.assertEqual(before, self.snapshot())
        self.assertFalse((staging / "overview").is_symlink())

    def test_capture_proposal_retry_recovers_a_committed_receipt_without_duplicate_media(self) -> None:
        prepared = self.prepare()
        proposal_id = prepared["proposal"]["proposal_id"]
        capture_id = prepared["capture"]["capture_session_id"]
        environment = dict(os.environ)
        environment["PROPERTY_INVENTORY_FAIL_PROPOSAL_AFTER_COMMIT"] = "1"
        crashed = subprocess.run(
            self.command("proposal-apply", proposal_id), text=True, capture_output=True,
            check=False, env=environment,
        )
        self.assertEqual(crashed.returncode, 99)
        staging = self.runtime / "capture-staging" / capture_id
        staging.rename(self.runtime / f"moved-{capture_id}")
        recovered = self.cli("proposal-apply", proposal_id)
        self.assertEqual(recovered["status"], "recovered_applied")
        self.assertEqual(len([path for path in self.media.rglob("*") if path.is_file()]), 2)

    def test_applied_staging_cleanup_never_deletes_unexpected_bytes(self) -> None:
        prepared = self.prepare()
        proposal_id = prepared["proposal"]["proposal_id"]
        capture_id = prepared["capture"]["capture_session_id"]
        environment = dict(os.environ)
        environment["PROPERTY_INVENTORY_FAIL_PROPOSAL_AFTER_COMMIT"] = "1"
        crashed = subprocess.run(
            self.command("proposal-apply", proposal_id),
            text=True,
            capture_output=True,
            check=False,
            env=environment,
        )
        self.assertEqual(crashed.returncode, 99)
        staging = self.runtime / "capture-staging" / capture_id
        retired = staging.parent / f".applied-{capture_id}"
        staging.rename(retired)
        unexpected = retired / "UNEXPECTED-USER-BYTES"
        unexpected.write_bytes(b"must survive")

        recovered = self.cli("proposal-apply", proposal_id)
        self.assertEqual(recovered["status"], "recovered_applied")
        self.assertEqual(recovered["capture_staging_cleanup"], "unsafe_retained")
        self.assertEqual(unexpected.read_bytes(), b"must survive")
        self.assertEqual(self.cli("capture-status", capture_id)["status"], "applied")

    def test_applied_staging_cleanup_recovers_after_partial_unlink(self) -> None:
        prepared = self.prepare()
        proposal_id = prepared["proposal"]["proposal_id"]
        capture_id = prepared["capture"]["capture_session_id"]
        environment = dict(os.environ)
        environment["PROPERTY_INVENTORY_FAIL_CAPTURE_CLEANUP"] = "after-first-file"
        crashed = subprocess.run(
            self.command("proposal-apply", proposal_id),
            text=True,
            capture_output=True,
            check=False,
            env=environment,
        )
        self.assertEqual(crashed.returncode, 93)
        staging_parent = self.runtime / "capture-staging"
        retired = staging_parent / f".applied-{capture_id}"
        self.assertTrue(retired.is_dir())
        self.assertFalse((staging_parent / capture_id).exists())

        cleanup = self.cli("capture-cleanup", capture_id)
        self.assertEqual(cleanup["staging_cleanup"], "removed")
        self.assertFalse(retired.exists())
        recovered = self.cli("proposal-apply", proposal_id)
        self.assertEqual(recovered["status"], "recovered_applied")
        self.assertEqual(recovered["capture_staging_cleanup"], "already_absent")
        status = self.cli("capture-status", capture_id)
        self.assertEqual(status["status"], "applied")

    def test_media_install_crash_is_journaled_and_retry_leaves_no_orphan(self) -> None:
        prepared = self.prepare()
        proposal_id = prepared["proposal"]["proposal_id"]
        environment = dict(os.environ)
        environment["PROPERTY_INVENTORY_FAIL_CAPTURE_AFTER_MEDIA"] = "1"
        crashed = subprocess.run(
            self.command("proposal-apply", proposal_id),
            text=True,
            capture_output=True,
            check=False,
            env=environment,
        )
        self.assertEqual(crashed.returncode, 97)
        journal = self.runtime / ".property-inventory-capture-media.json"
        self.assertTrue(journal.is_file())
        journal_data = json.loads(journal.read_text(encoding="utf-8"))
        digest = journal_data["entries"][0]["digest"]
        destination = self.media / "sha256" / digest[:2] / digest
        destination.unlink(missing_ok=True)
        stale = destination.with_name(
            f".{digest}.write-00000000-0000-4000-8000-000000000000"
        )
        stale.write_bytes(b"partial private media")
        self.assertEqual(len((self.store / "capture_sessions.jsonl").read_text().splitlines()), 0)
        self.assertEqual(len((self.store / "proposal_commits.jsonl").read_text().splitlines()), 0)
        recovered = self.cli("proposal-apply", proposal_id)
        self.assertEqual(recovered["status"], "committed_to_store")
        self.assertFalse(stale.exists())
        self.assertEqual(hashlib.sha256(destination.read_bytes()).hexdigest(), digest)
        self.assertFalse(journal.exists())
        self.assertFalse((self.runtime / ".property-inventory-capture-media").exists())
        self.assertEqual(len([path for path in self.media.rglob("*") if path.is_file()]), 2)

    def test_prejournal_media_staging_crash_is_safely_discarded_on_retry(self) -> None:
        prepared = self.prepare()
        proposal_id = prepared["proposal"]["proposal_id"]
        environment = dict(os.environ)
        environment["PROPERTY_INVENTORY_FAIL_CAPTURE_BEFORE_JOURNAL"] = "1"
        crashed = subprocess.run(
            self.command("proposal-apply", proposal_id),
            text=True,
            capture_output=True,
            check=False,
            env=environment,
        )
        self.assertEqual(crashed.returncode, 96)
        self.assertFalse((self.runtime / ".property-inventory-capture-media.json").exists())
        self.assertTrue((self.runtime / ".property-inventory-capture-media").is_dir())
        recovered = self.cli("proposal-apply", proposal_id)
        self.assertEqual(recovered["status"], "committed_to_store")
        self.assertFalse((self.runtime / ".property-inventory-capture-media").exists())
        self.assertEqual(len([path for path in self.media.rglob("*") if path.is_file()]), 2)

    def test_capture_media_recovery_journal_is_bounded_and_never_blocks(self) -> None:
        journal = self.runtime / ".property-inventory-capture-media.json"
        workspace = self.runtime / ".property-inventory-capture-media"
        for entry_kind in ("symlink", "fifo", "oversize"):
            with self.subTest(entry_kind=entry_kind):
                workspace.mkdir()
                if entry_kind == "symlink":
                    journal.symlink_to(self.overview)
                elif entry_kind == "fifo":
                    os.mkfifo(journal)
                else:
                    with journal.open("wb") as handle:
                        handle.truncate(8 * 1024 * 1024 + 1)
                completed = subprocess.run(
                    self.command("status"),
                    text=True,
                    capture_output=True,
                    check=False,
                    timeout=2,
                )
                self.assertNotEqual(completed.returncode, 0)
                error = json.loads(completed.stderr)["error"]
                self.assertTrue(
                    "symlink" in error
                    or "regular file" in error
                    or "too large" in error,
                    error,
                )
                journal.unlink()
                workspace.rmdir()

    def test_existing_proposal_media_uses_verified_hardlinks_on_same_filesystem(self) -> None:
        source_root = self.scratch / "existing-media"
        destination_root = self.scratch / "proposal-media"
        payload = b"immutable existing media"
        digest = hashlib.sha256(payload).hexdigest()
        source = source_root / "sha256" / digest[:2] / digest
        source.parent.mkdir(parents=True)
        source.write_bytes(payload)
        with patch.object(
            inventory_cli,
            "durable_copy",
            side_effect=AssertionError("same-filesystem media must not be byte-copied"),
        ):
            destination = inventory_cli.materialize_verified_existing_media(
                source_root,
                destination_root,
                {"sha256": digest, "byte_size": len(payload)},
            )
        self.assertEqual(destination.read_bytes(), payload)
        self.assertEqual(source.stat().st_ino, destination.stat().st_ino)

    def test_failed_named_adapter_never_stages_or_mutates(self) -> None:
        before = self.snapshot()
        failed = self.failed(
            "capture-prepare", "--overview", str(self.overview), "--captured-on", "2026-08-06",
            "--segments", '[{"segment_id":"label","region":{"x":2,"y":1,"width":5,"height":4}}]',
            "--source-ref", "Broken adapter image", "--adapter-name", "broken",
        )
        self.assertIn("adapter execution failed", failed["error"])
        self.assertEqual(before, self.snapshot())
        self.assertFalse((self.runtime / "capture-staging").exists())

    def test_adapter_can_segment_one_overview_without_caller_rectangles(self) -> None:
        prepared = self.cli(
            "capture-prepare",
            "--overview",
            str(self.overview),
            "--captured-on",
            "2026-08-06",
            "--segments",
            "[]",
            "--source-ref",
            "Adapter-segmented overview",
            "--adapter-name",
            "fixture",
        )
        capture = prepared["capture"]
        self.assertEqual(capture["segmentation_source"], "adapter")
        self.assertEqual(capture["segments"][0]["segment_id"], "detected-object")
        staging = self.runtime / "capture-staging" / capture["capture_session_id"]
        artifact = json.loads((staging / "artifact.json").read_text(encoding="utf-8"))
        self.assertEqual(len(artifact["crops"]), 1)
        caller_selected = self.cli(
            "capture-prepare",
            "--overview",
            str(self.overview),
            "--captured-on",
            "2026-08-06",
            "--segments",
            '[{"segment_id":"caller-region","region":{"x":0,"y":0,"width":2,"height":2}}]',
            "--source-ref",
            "Caller-selected segment wins",
            "--adapter-name",
            "fixture",
        )["capture"]
        self.assertEqual(caller_selected["segmentation_source"], "supplied")
        self.assertEqual(caller_selected["segments"], [
            {
                "segment_id": "caller-region",
                "region": {"x": 0, "y": 0, "width": 2, "height": 2},
            }
        ])
        staging_count = len(list((self.runtime / "capture-staging").iterdir()))
        no_prediction = self.failed(
            "capture-prepare",
            "--overview",
            str(self.overview),
            "--captured-on",
            "2026-08-06",
            "--segments",
            "[]",
            "--source-ref",
            "Adapter abstained from segmentation",
            "--adapter-name",
            "identifier",
        )
        self.assertIn("returns predicted segments", no_prediction["error"])
        self.assertEqual(
            len(list((self.runtime / "capture-staging").iterdir())),
            staging_count,
        )
        failed = self.failed(
            "capture-prepare",
            "--overview",
            str(self.overview),
            "--captured-on",
            "2026-08-06",
            "--segments",
            "[]",
            "--source-ref",
            "No segmentation engine",
        )
        self.assertIn("non-empty JSON array", failed["error"])

    def test_capture_prepare_recovers_partial_and_lost_response_without_duplicates(self) -> None:
        arguments = (
            "capture-prepare",
            "--overview",
            str(self.overview),
            "--captured-on",
            "2026-08-06",
            "--segments",
            '[{"segment_id":"label","region":{"x":2,"y":1,"width":5,"height":4}}]',
            "--source-ref",
            "Atomic prepare crash fixture",
        )
        environment = dict(os.environ)
        environment["PROPERTY_INVENTORY_FAIL_CAPTURE_PREPARE"] = "after-first-file"
        partial = subprocess.run(
            self.command(*arguments),
            text=True,
            capture_output=True,
            check=False,
            env=environment,
        )
        self.assertEqual(partial.returncode, 95)
        stage_parent = self.runtime / "capture-staging"
        self.assertEqual(
            len(
                [
                    entry
                    for entry in stage_parent.iterdir()
                    if entry.name.startswith(".capture-build-")
                ]
            ),
            1,
        )
        self.assertFalse(
            any(entry.name.startswith("capture-") for entry in stage_parent.iterdir())
        )
        recovered = self.cli(*arguments)["capture"]
        self.assertFalse(
            any(
                entry.name.startswith(".capture-build-")
                for entry in stage_parent.iterdir()
            )
        )
        self.assertEqual(
            self.cli("capture-status", recovered["capture_session_id"])["status"],
            "awaiting_review",
        )

        lost_arguments = [*arguments]
        lost_arguments[-1] = "Published prepare with lost response"
        lost_arguments.extend(("--adapter-name", "nondeterministic"))
        environment["PROPERTY_INVENTORY_FAIL_CAPTURE_PREPARE"] = "after-publish"
        published = subprocess.run(
            self.command(*lost_arguments),
            text=True,
            capture_output=True,
            check=False,
            env=environment,
        )
        self.assertEqual(published.returncode, 94)
        sessions_before_retry = {
            entry.name
            for entry in stage_parent.iterdir()
            if entry.name.startswith("capture-")
        }
        self.assertEqual(len(sessions_before_retry), 2)
        published_session = next(
            session
            for session in sessions_before_retry
            if session != recovered["capture_session_id"]
        )
        published_artifact = json.loads(
            (
                stage_parent / published_session / "artifact.json"
            ).read_text(encoding="utf-8")
        )
        retried = self.cli(*lost_arguments)["capture"]
        self.assertEqual(retried["capture_session_id"], published_session)
        self.assertEqual(
            retried["observations"],
            published_artifact["observations"],
        )
        self.assertEqual(
            {
                entry.name
                for entry in stage_parent.iterdir()
                if entry.name.startswith("capture-")
            },
            sessions_before_retry,
        )

        adapter_config = json.loads(self.adapter_config.read_text(encoding="utf-8"))
        adapter_config["adapters"]["nondeterministic"]["revision"] = (
            "test:nondeterministic:2"
        )
        self.adapter_config.write_text(
            json.dumps(adapter_config), encoding="utf-8"
        )
        revised = self.cli(*lost_arguments)["capture"]
        self.assertNotEqual(revised["capture_session_id"], published_session)
        revised_artifact = json.loads(
            (
                stage_parent
                / revised["capture_session_id"]
                / "artifact.json"
            ).read_text(encoding="utf-8")
        )
        self.assertEqual(
            revised_artifact["adapter"],
            {
                "name": "nondeterministic",
                "revision": "test:nondeterministic:2",
                "command_sha256": published_artifact["adapter"]["command_sha256"],
            },
        )

    def test_source_created_capture_cannot_masquerade_as_physical_check(self) -> None:
        failed = self.failed(
            "capture-prepare",
            "--overview",
            str(self.overview),
            "--captured-on",
            "2026-08-06",
            "--segments",
            '[{"segment_id":"label","region":{"x":2,"y":1,"width":5,"height":4}}]',
            "--source-ref",
            "Overview is not possession proof",
            "--evidence-type",
            "physical_check",
        )
        self.assertIn("cannot assert physical_check", failed["error"])
        self.assertFalse((self.runtime / "capture-staging").exists())

    def test_capture_date_cannot_be_laundered_into_a_later_physical_check(self) -> None:
        self.cli(
            "add-location", "--location-id", "loc-capture", "--name", "Capture shelf",
            "--kind", "room", "--sensitivity", "low",
        )
        staged = self.cli(
            "capture-prepare", "--overview", str(self.overview),
            "--captured-on", "2020-01-02",
            "--segments", '[{"segment_id":"label","region":{"x":2,"y":1,"width":5,"height":4}}]',
            "--source-ref", "Historical overview",
        )["capture"]
        staging = self.runtime / "capture-staging" / staged["capture_session_id"]
        artifact = json.loads((staging / "artifact.json").read_text(encoding="utf-8"))
        crop = artifact["crops"][0]
        decisions = [
            {
                "crop_id": crop["crop_id"],
                "segment_id": "label",
                "observation_id": None,
                "item_id": None,
                "discovery": {
                    "name": "Historical-photo tool",
                    "category": "tool",
                    "new_model": True,
                    "new_unit": True,
                    "sensitivity": "low",
                    "specs": {},
                    "identifiers": {},
                },
                "physical": {
                    "actor": "Capture integration",
                    "checked_on": "2026-08-06",
                    "condition": None,
                    "container_id": None,
                    "location_id": "loc-capture",
                    "notes": None,
                    "quantity": 1,
                    "serial_or_lot": None,
                    "unit": "item",
                },
            }
        ]
        before = self.snapshot()
        failed = self.failed(
            "capture-review", staged["capture_session_id"],
            "--artifact-sha256", staged["artifact_sha256"],
            "--links", "{}", "--decisions", json.dumps(decisions),
        )
        self.assertIn("physical check date disagrees with capture date", failed["error"])
        self.assertEqual(before, self.snapshot())
        self.assertFalse((staging / "review.json").exists())

    def test_review_cannot_credit_adapter_serial_or_barcode_from_another_crop(self) -> None:
        self.cli(
            "add-location", "--location-id", "loc-capture", "--name", "Capture shelf",
            "--kind", "room", "--sensitivity", "low",
        )
        staged = self.cli(
            "capture-prepare", "--overview", str(self.overview),
            "--captured-on", "2026-08-06", "--adapter-name", "fixture",
            "--segments", '[{"segment_id":"serial-crop","region":{"x":1,"y":2,"width":3,"height":4}},{"segment_id":"barcode-crop","region":{"x":5,"y":2,"width":2,"height":2}}]',
            "--source-ref", "Two label crops for adversarial review",
        )
        capture = staged["capture"]
        artifact = json.loads(
            (self.runtime / "capture-staging" / capture["capture_session_id"] / "artifact.json").read_text()
        )
        crops = {crop["segment_id"]: crop for crop in artifact["crops"]}
        physical = {
            "actor": "Capture integration", "checked_on": "2026-08-06",
            "condition": None, "container_id": None, "location_id": "loc-capture",
            "notes": None, "quantity": None, "serial_or_lot": None, "unit": None,
        }
        for crop_segment, observation_id in (
            ("serial-crop", "observation-2"),
            ("barcode-crop", "observation-1"),
        ):
            with self.subTest(crop_segment=crop_segment, observation_id=observation_id):
                crop = crops[crop_segment]
                decision = [{
                    "crop_id": crop["crop_id"], "segment_id": crop_segment,
                    "observation_id": observation_id, "item_id": None,
                    "discovery": {
                        "name": f"Miscredited {observation_id}", "category": "tool",
                        "new_model": True, "new_unit": True, "sensitivity": "low",
                        "specs": {}, "identifiers": {},
                    },
                    "physical": physical,
                }]
                failed = self.failed(
                    "capture-review", capture["capture_session_id"], "--artifact-sha256",
                    capture["artifact_sha256"], "--links", "{}", "--decisions", json.dumps(decision),
                )
                self.assertIn("not fully contained by its crop", failed["error"])
                self.assertFalse(
                    (self.runtime / "capture-staging" / capture["capture_session_id"] / "review.json").exists()
                )

    def test_review_cannot_credit_a_manual_observation_from_another_crop(self) -> None:
        self.cli(
            "add-location", "--location-id", "loc-capture", "--name", "Capture shelf",
            "--kind", "room", "--sensitivity", "low",
        )
        staged = self.cli(
            "capture-prepare", "--overview", str(self.overview),
            "--captured-on", "2026-08-06",
            "--segments", '[{"segment_id":"crop-a","region":{"x":0,"y":0,"width":4,"height":4}},{"segment_id":"crop-b","region":{"x":5,"y":0,"width":4,"height":4}}]',
            "--source-ref", "Two manual crops for adversarial review",
        )
        capture = staged["capture"]
        artifact = json.loads(
            (self.runtime / "capture-staging" / capture["capture_session_id"] / "artifact.json").read_text()
        )
        crops = {crop["segment_id"]: crop for crop in artifact["crops"]}
        manual = {
            "manual-crop-b": {
                "crop_id": crops["crop-b"]["crop_id"], "segment_id": "crop-b",
                "observation": {"serial": "from crop B"},
            }
        }
        decision = [{
            "crop_id": crops["crop-a"]["crop_id"], "segment_id": "crop-a",
            "observation_id": "manual-crop-b", "item_id": None,
            "discovery": {
                "name": "Miscredited manual serial", "category": "tool",
                "new_model": True, "new_unit": True, "sensitivity": "low",
                "specs": {}, "identifiers": {},
            },
            "physical": {
                "actor": "Capture integration", "checked_on": "2026-08-06",
                "condition": None, "container_id": None, "location_id": "loc-capture",
                "notes": None, "quantity": None, "serial_or_lot": None, "unit": None,
            },
        }]
        failed = self.failed(
            "capture-review", capture["capture_session_id"], "--artifact-sha256",
            capture["artifact_sha256"], "--links", "{}", "--manual-observations",
            json.dumps(manual), "--decisions", json.dumps(decision),
        )
        self.assertIn("manual observation disagrees with its crop", failed["error"])

    def test_manual_crop_discovery_creates_bound_physical_verification_and_retries_once(self) -> None:
        self.cli(
            "add-location", "--location-id", "loc-capture", "--name", "Capture shelf",
            "--kind", "room", "--sensitivity", "high",
        )
        staged = self.cli(
            "capture-prepare", "--overview", str(self.overview),
            "--captured-on", "2026-08-06",
            "--segments", '[{"segment_id":"label","region":{"x":2,"y":1,"width":5,"height":4}}]',
            "--source-ref", "Overview plus manual label inspection",
        )
        capture = staged["capture"]
        staging = self.runtime / "capture-staging" / capture["capture_session_id"]
        artifact = json.loads((staging / "artifact.json").read_text(encoding="utf-8"))
        crop = artifact["crops"][0]
        manual = {
            "manual-label": {
                "crop_id": crop["crop_id"], "segment_id": "label",
                "observation": {"label": "Acme adjustable spanner", "manual": True},
            }
        }
        physical = {
            "actor": "Capture integration", "checked_on": "2026-08-06",
            "condition": "good", "container_id": None, "location_id": "loc-capture",
            "notes": "Read directly from the label.", "quantity": 1, "serial_or_lot": None,
            "unit": "item",
        }
        decisions = [{
            "crop_id": crop["crop_id"], "segment_id": "label",
            "observation_id": "manual-label", "item_id": None,
            "discovery": {
                "name": "Acme adjustable spanner", "category": "hand tool",
                "new_model": True, "new_unit": True, "sensitivity": "low",
                "specs": {}, "identifiers": {},
            },
            "physical": physical,
        }]
        reviewed = self.cli(
            "capture-review", capture["capture_session_id"], "--artifact-sha256",
            capture["artifact_sha256"], "--links", "{}", "--manual-observations",
            json.dumps(manual), "--decisions", json.dumps(decisions),
        )
        environment = dict(os.environ)
        environment["PROPERTY_INVENTORY_FAIL_PROPOSAL_AFTER_COMMIT"] = "1"
        crashed = subprocess.run(
            self.command("proposal-apply", reviewed["proposal"]["proposal_id"]), text=True,
            capture_output=True, check=False, env=environment,
        )
        self.assertEqual(crashed.returncode, 99)
        self.assertEqual(
            self.cli("proposal-apply", reviewed["proposal"]["proposal_id"])["status"],
            "recovered_applied",
        )
        items = [json.loads(line) for line in (self.store / "items.jsonl").read_text().splitlines()]
        self.assertEqual(len(items), 1)
        item = items[0]
        self.assertEqual((item["ownership_state"], item["location_id"], item["verified_on"]),
                         ("confirmed", "loc-capture", "2026-08-06"))
        evidence = [json.loads(line) for line in (self.store / "evidence.jsonl").read_text().splitlines()]
        physical_evidence = next(row for row in evidence if row["evidence_type"] == "physical_check")
        self.assertEqual(physical_evidence["claim_strength"], "explicit_current")
        self.assertEqual(physical_evidence["sensitivity"], "high")
        assets = [json.loads(line) for line in (self.store / "evidence_assets.jsonl").read_text().splitlines()]
        crop_asset = next(row for row in assets if row["evidence_id"] == physical_evidence["evidence_id"])
        self.assertEqual(crop_asset["role"], "crop")
        self.assertEqual(json.loads(crop_asset["region_json"]), crop["region"])
        self.assertEqual(
            {json.loads(line)["sensitivity"] for line in (self.store / "media_assets.jsonl").read_text().splitlines()},
            {"high"},
        )
        events = [json.loads(line) for line in (self.store / "inventory_events.jsonl").read_text().splitlines()]
        self.assertEqual(
            {row["event_type"] for row in events if row["item_id"] == item["item_id"]},
            {"received", "physically_verified"},
        )
        self.assertEqual(self.cli("status")["verification"]["failures"], [])

    def test_existing_model_capture_replays_every_sealed_model_fact(self) -> None:
        self.cli(
            "add-location", "--location-id", "loc-existing-model",
            "--name", "Existing model shelf", "--kind", "room",
            "--sensitivity", "low",
        )
        ordered = self.cli(
            "order", "--actor", "Capture integration",
            "--source-ref", "Existing model fixture", "--name", "Reusable model",
            "--brand", "Acme", "--model", "R1", "--category", "tool",
            "--specs", '{"size":"M"}', "--identifiers", '{"sku":"R1"}',
            "--ordered-on", "2026-08-05", "--order-placed", "--new-model",
            "--sensitivity", "low",
        )["result"]
        model_id = self.cli("show", ordered["item_id"])["item"]["model_id"]
        staged = self.cli(
            "capture-prepare", "--overview", str(self.overview),
            "--captured-on", "2026-08-06",
            "--segments", '[{"segment_id":"model","region":{"x":1,"y":1,"width":4,"height":4}}]',
            "--source-ref", "Existing-model overview",
        )["capture"]
        artifact = json.loads(
            (
                self.runtime / "capture-staging" / staged["capture_session_id"]
                / "artifact.json"
            ).read_text(encoding="utf-8")
        )
        crop = artifact["crops"][0]
        decision = [{
            "crop_id": crop["crop_id"], "segment_id": "model",
            "observation_id": None, "item_id": None,
            "discovery": {
                "name": "Reusable model", "brand": "Acme", "model": "R1",
                "category": "tool", "existing_model_id": model_id,
                "new_model": False, "new_unit": True, "sensitivity": "low",
                "specs": {"size": "M"}, "identifiers": {"sku": "R1"},
            },
            "physical": {
                "actor": "Capture integration", "checked_on": "2026-08-06",
                "condition": "working", "container_id": None,
                "location_id": "loc-existing-model", "notes": None,
                "quantity": 1, "serial_or_lot": None, "unit": "item",
            },
        }]
        reviewed = self.cli(
            "capture-review", staged["capture_session_id"], "--artifact-sha256",
            staged["artifact_sha256"], "--links", "{}", "--decisions",
            json.dumps(decision),
        )
        self.cli("proposal-apply", reviewed["proposal"]["proposal_id"])
        models_path = self.store / "models.jsonl"
        original = models_path.read_bytes()
        models = [json.loads(line) for line in original.decode().splitlines()]
        model = next(row for row in models if row["model_id"] == model_id)
        model["name"] = "Forged existing model"
        models_path.write_text(
            "".join(json.dumps(row, sort_keys=True) + "\n" for row in models),
            encoding="utf-8",
        )
        self.assertIn("capture provenance", self.failed("status")["error"])
        models_path.write_bytes(original)
        self.assertEqual(self.cli("status")["verification"]["failures"], [])
        session_path = self.store / "capture_sessions.jsonl"
        original_sessions = session_path.read_bytes()

        def mutate_review(path: tuple[str, ...], value: object) -> None:
            sessions = [json.loads(line) for line in original_sessions.decode().splitlines()]
            session = sessions[0]
            review = json.loads(session["review_json"])
            target: object = review
            for key in path[:-1]:
                target = target[key]  # type: ignore[index]
            target[path[-1]] = value  # type: ignore[index]
            review_wire = json.dumps(
                review, ensure_ascii=False, indent=2, sort_keys=True
            ) + "\n"
            session["review_json"] = review_wire
            session["review_sha256"] = hashlib.sha256(review_wire.encode()).hexdigest()
            session_path.write_text(
                "".join(json.dumps(row, sort_keys=True) + "\n" for row in sessions),
                encoding="utf-8",
            )

        for path, value in (
            (("decisions", 0, "physical", "actor"), "Forged actor"),
            (("decisions", 0, "physical", "notes"), "Forged notes"),
            (("decisions", 0, "physical", "condition"), "broken"),
            (("decisions", 0, "physical", "quantity"), 2),
            (("decisions", 0, "physical", "serial_or_lot"), "forged-serial"),
            (("decisions", 0, "discovery", "brand"), "Forged brand"),
            (("decisions", 0, "discovery", "model"), "Forged model"),
            (("decisions", 0, "discovery", "identifiers"), {"barcode": "forged"}),
            (("decisions", 0, "discovery", "specs"), {"size": "forged"}),
        ):
            with self.subTest(path=path):
                mutate_review(path, value)
                self.assertIn("capture provenance", self.failed("status")["error"])
                session_path.write_bytes(original_sessions)
        self.assertEqual(self.cli("status")["verification"]["failures"], [])

    def test_capture_review_reconciles_existing_item_and_rejects_an_unrelated_crop(self) -> None:
        self.cli(
            "add-location", "--location-id", "loc-capture", "--name", "Capture shelf",
            "--kind", "room", "--sensitivity", "low",
        )
        item_id = self.cli(
            "order", "--actor", "Capture integration", "--source-ref", "Order record",
            "--name", "Existing capture item", "--category", "hand tool",
            "--ordered-on", "2026-08-05", "--order-placed",
        )["result"]["item_id"]
        staged = self.cli(
            "capture-prepare", "--overview", str(self.overview),
            "--captured-on", "2026-08-06",
            "--segments", '[{"segment_id":"first","region":{"x":0,"y":0,"width":4,"height":4}},{"segment_id":"second","region":{"x":5,"y":0,"width":4,"height":4}}]',
            "--source-ref", "Existing item physical check",
        )
        capture = staged["capture"]
        artifact = json.loads((self.runtime / "capture-staging" / capture["capture_session_id"] / "artifact.json").read_text())
        first_crop, second_crop = artifact["crops"]
        physical = {
            "actor": "Capture integration", "checked_on": "2026-08-06",
            "condition": "used", "container_id": None, "location_id": "loc-capture",
            "notes": None, "quantity": 2, "serial_or_lot": "lot-42", "unit": "item",
        }
        forged = [{
            "crop_id": first_crop["crop_id"], "segment_id": "second",
            "observation_id": None, "item_id": item_id, "discovery": None, "physical": physical,
        }]
        failed = self.failed(
            "capture-review", capture["capture_session_id"], "--artifact-sha256",
            capture["artifact_sha256"], "--links", "{}", "--decisions", json.dumps(forged),
        )
        self.assertIn("crop disagrees with segment", failed["error"])
        decisions = [{**forged[0], "segment_id": "first"}]
        reviewed = self.cli(
            "capture-review", capture["capture_session_id"], "--artifact-sha256",
            capture["artifact_sha256"], "--links", "{}", "--decisions", json.dumps(decisions),
        )
        self.cli("proposal-apply", reviewed["proposal"]["proposal_id"])
        items = [json.loads(line) for line in (self.store / "items.jsonl").read_text().splitlines()]
        self.assertEqual(len(items), 1)
        item = items[0]
        self.assertEqual((item["quantity"], item["condition"], item["serial_or_lot"], item["location_id"]),
                         (2, "used", "lot-42", "loc-capture"))
        quantity_events = [
            json.loads(line)
            for line in (self.store / "inventory_events.jsonl").read_text().splitlines()
            if json.loads(line)["event_type"] == "quantity_changed"
        ]
        self.assertEqual(len(quantity_events), 1)
        amendments = [
            json.loads(line)
            for line in (self.store / "item_detail_amendments.jsonl").read_text().splitlines()
        ]
        self.assertEqual(len(amendments), 1)
        self.assertEqual(
            json.loads(amendments[0]["changes_json"]),
            {"condition": "used", "serial_or_lot": "lot-42"},
        )
        evidence = [json.loads(line) for line in (self.store / "evidence.jsonl").read_text().splitlines()]
        physical_evidence = next(row for row in evidence if row["evidence_type"] == "physical_check")
        assets = [json.loads(line) for line in (self.store / "evidence_assets.jsonl").read_text().splitlines()]
        bound = next(row for row in assets if row["evidence_id"] == physical_evidence["evidence_id"])
        self.assertEqual(json.loads(bound["region_json"]), first_crop["region"])
        self.assertEqual(self.cli("status")["verification"]["failures"], [])
        self.cli(
            "change", "--actor", "Capture integration", "--source-ref", "Loaned out",
            "--item-id", item_id, "--event-type", "lent", "--occurred-on", "2026-08-07",
        )
        lent_capture = self.cli(
            "capture-prepare", "--overview", str(self.overview),
            "--captured-on", "2026-08-08",
            "--segments", '[{"segment_id":"lent-label","region":{"x":0,"y":0,"width":4,"height":4}}]',
            "--source-ref", "Lent item direct physical check",
        )["capture"]
        lent_artifact = json.loads(
            (self.runtime / "capture-staging" / lent_capture["capture_session_id"] / "artifact.json").read_text()
        )
        lent_physical = {**physical, "checked_on": "2026-08-08", "quantity": None, "unit": None}
        lent_decision = [{
            "crop_id": lent_artifact["crops"][0]["crop_id"], "segment_id": "lent-label",
            "observation_id": None, "item_id": item_id, "discovery": None,
            "physical": lent_physical,
        }]
        lent_review = self.cli(
            "capture-review", lent_capture["capture_session_id"], "--artifact-sha256",
            lent_capture["artifact_sha256"], "--links", "{}", "--decisions", json.dumps(lent_decision),
        )
        self.cli("proposal-apply", lent_review["proposal"]["proposal_id"])
        current_item = next(
            json.loads(line) for line in (self.store / "items.jsonl").read_text().splitlines()
            if json.loads(line)["item_id"] == item_id
        )
        self.assertEqual(current_item["ownership_state"], "lent")
        self.assertEqual(self.cli("status")["verification"]["failures"], [])
        evidence_asset_rows = [
            json.loads(line)
            for line in (self.store / "evidence_assets.jsonl").read_text().splitlines()
        ]
        next(
            row for row in evidence_asset_rows
            if row["evidence_id"] == physical_evidence["evidence_id"]
            and row["role"] == "crop"
        )["region_json"] = json.dumps(second_crop["region"], sort_keys=True, separators=(",", ":"))
        (self.store / "evidence_assets.jsonl").write_text(
            "".join(json.dumps(row, sort_keys=True) + "\n" for row in evidence_asset_rows),
            encoding="utf-8",
        )
        self.assertIn("capture provenance", self.failed("status")["error"])

    def test_bound_capture_replays_discovery_sensitivity_after_review_hash_rewrite(self) -> None:
        self.cli(
            "add-location", "--location-id", "loc-capture", "--name", "Capture shelf",
            "--kind", "room", "--sensitivity", "low",
        )
        staged = self.cli(
            "capture-prepare", "--overview", str(self.overview),
            "--captured-on", "2026-08-06",
            "--segments", '[{"segment_id":"label","region":{"x":2,"y":1,"width":5,"height":4}}]',
            "--source-ref", "Low-sensitivity discovery capture",
        )
        capture = staged["capture"]
        artifact = json.loads(
            (self.runtime / "capture-staging" / capture["capture_session_id"] / "artifact.json").read_text()
        )
        crop = artifact["crops"][0]
        decision = [{
            "crop_id": crop["crop_id"], "segment_id": "label",
            "observation_id": None, "item_id": None,
            "discovery": {
                "name": "Sensitivity replay tool", "category": "tool", "new_model": True,
                "new_unit": True, "sensitivity": "low", "specs": {}, "identifiers": {},
            },
            "physical": {
                "actor": "Capture integration", "checked_on": "2026-08-06",
                "condition": "good", "container_id": None, "location_id": "loc-capture",
                "notes": None, "quantity": 1, "serial_or_lot": None, "unit": "item",
            },
        }]
        reviewed = self.cli(
            "capture-review", capture["capture_session_id"], "--artifact-sha256",
            capture["artifact_sha256"], "--links", "{}", "--decisions", json.dumps(decision),
        )
        self.cli("proposal-apply", reviewed["proposal"]["proposal_id"])
        session_path = self.store / "capture_sessions.jsonl"
        sessions = [json.loads(line) for line in session_path.read_text().splitlines()]
        review = json.loads(sessions[0]["review_json"])
        review["decisions"][0]["discovery"]["sensitivity"] = "high"
        review_wire = json.dumps(review, ensure_ascii=False, indent=2, sort_keys=True) + "\n"
        sessions[0]["review_json"] = review_wire
        sessions[0]["review_sha256"] = hashlib.sha256(review_wire.encode()).hexdigest()
        session_path.write_text(
            "".join(json.dumps(row, sort_keys=True) + "\n" for row in sessions),
            encoding="utf-8",
        )
        self.assertIn("capture provenance", self.failed("status")["error"])

    def test_adapter_observations_rank_visible_duplicate_candidates_without_linking(self) -> None:
        ordered = self.cli(
            "order", "--actor", "Capture test", "--source-ref", "Recorded model AB-1",
            "--name", "Model AB-1", "--category", "tool", "--ordered-on", "2026-08-06",
            "--order-placed",
        )["result"]
        prepared = self.cli(
            "capture-prepare", "--overview", str(self.overview), "--captured-on", "2026-08-06",
            "--segments", '[{"segment_id":"label","region":{"x":2,"y":1,"width":5,"height":4}}]',
            "--source-ref", "Adapter overview", "--adapter-name", "fixture",
        )
        candidates = prepared["capture"]["duplicate_candidates"]
        self.assertEqual(len(prepared["capture"]["observations"]), 2)
        self.assertIn(ordered["item_id"], [row["item_id"] for row in candidates["observation-1"]])
        self.assertEqual(len((self.store / "capture_sessions.jsonl").read_text().splitlines()), 0)
        capture_id = prepared["capture"]["capture_session_id"]
        artifact_path = self.runtime / "capture-staging" / capture_id / "artifact.json"
        artifact_digest = hashlib.sha256(artifact_path.read_bytes()).hexdigest()
        items_before = (self.store / "items.jsonl").read_bytes()
        events_before = (self.store / "inventory_events.jsonl").read_bytes()
        reviewed = self.cli(
            "capture-review",
            capture_id,
            "--artifact-sha256",
            prepared["capture"]["artifact_sha256"],
            "--links",
            json.dumps({"observation-1": ordered["item_id"]}),
        )
        self.assertEqual(hashlib.sha256(artifact_path.read_bytes()).hexdigest(), artifact_digest)
        self.cli("proposal-apply", reviewed["proposal"]["proposal_id"])
        self.assertEqual((self.store / "items.jsonl").read_bytes(), items_before)
        self.assertEqual(
            (self.store / "inventory_events.jsonl").read_bytes(), events_before
        )
        links = (self.store / "item_evidence.jsonl").read_text(encoding="utf-8")
        self.assertIn(ordered["item_id"], links)

    def test_adapter_decision_materializes_its_observation_link(self) -> None:
        self.cli(
            "add-location", "--location-id", "loc-adapter-decision",
            "--name", "Adapter decision shelf", "--kind", "room", "--sensitivity", "low",
        )
        prepared = self.cli(
            "capture-prepare", "--overview", str(self.overview),
            "--captured-on", "2026-08-06", "--adapter-name", "fixture",
            "--segments", '[{"segment_id":"detected-object","region":{"x":1,"y":2,"width":6,"height":4}}]',
            "--source-ref", "Adapter-backed physical review",
        )["capture"]
        staging = self.runtime / "capture-staging" / prepared["capture_session_id"]
        artifact = json.loads((staging / "artifact.json").read_text(encoding="utf-8"))
        crop = artifact["crops"][0]
        decisions = [
            {
                "crop_id": crop["crop_id"],
                "segment_id": "detected-object",
                "observation_id": "observation-1",
                "item_id": None,
                "discovery": {
                    "name": "Adapter-identified tool",
                    "category": "tool",
                    "new_model": True,
                    "new_unit": True,
                    "sensitivity": "low",
                    "specs": {},
                    "identifiers": {"observed_model": "AB-1"},
                },
                "physical": {
                    "actor": "Capture integration",
                    "checked_on": "2026-08-06",
                    "condition": "good",
                    "container_id": None,
                    "location_id": "loc-adapter-decision",
                    "notes": "Model text reviewed in the selected crop.",
                    "quantity": 1,
                    "serial_or_lot": None,
                    "unit": "item",
                },
            }
        ]
        reviewed = self.cli(
            "capture-review", prepared["capture_session_id"],
            "--artifact-sha256", prepared["artifact_sha256"],
            "--links", "{}", "--decisions", json.dumps(decisions),
        )
        applied = self.cli("proposal-apply", reviewed["proposal"]["proposal_id"])
        self.assertEqual(applied["status"], "committed_to_store")
        decision_item_id = applied["result"]["operations"][0]["result"]["decisions"][0][
            "item_id"
        ]
        observations = [
            json.loads(line)
            for line in (self.store / "capture_observations.jsonl").read_text().splitlines()
        ]
        self.assertEqual(observations[0]["item_id"], decision_item_id)
        self.assertIsNone(observations[1]["item_id"])
        self.assertEqual(self.cli("status")["verification"]["failures"], [])

    def test_private_write_mcp_exposes_preparation_and_private_status_only(self) -> None:
        self.assertTrue(MCP.is_file())

        async def scenario() -> None:
            async with Client(
                stdio_client(
                    StdioServerParameters(
                        command=str(MCP),
                        args=[
                            "--inventory-root", str(self.root), "--runtime-dir", str(self.runtime),
                            "--media-root", str(self.media), "--scope", "private", "--profile", "write",
                            "--capture-adapters-config", str(self.adapter_config),
                        ],
                        cwd=self.scratch,
                    )
                ),
                mode="legacy",
            ) as client:
                listed = await client.list_tools()
                names = {tool.name for tool in listed.tools}
                self.assertIn("prepare_overview_capture", names)
                self.assertIn("review_overview_capture", names)
                self.assertIn("capture_status", names)
                capture_schema = next(
                    tool.input_schema
                    for tool in listed.tools
                    if tool.name == "prepare_overview_capture"
                )
                self.assertEqual(
                    set(capture_schema.get("properties", {})),
                    {
                        "adapter_name",
                        "adapter_timeout",
                        "captured_on",
                        "evidence_id",
                        "evidence_type",
                        "overview_path",
                        "segments",
                        "sensitivity",
                        "source_ref",
                    },
                )
                review_schema = next(
                    tool.input_schema
                    for tool in listed.tools
                    if tool.name == "review_overview_capture"
                )
                self.assertEqual(
                    set(review_schema.get("properties", {})),
                    {
                        "artifact_sha256",
                        "capture_session_id",
                        "decisions",
                        "links",
                        "manual_observations",
                    },
                )
                self.adapter_config.write_text(
                    json.dumps(
                        {
                            "version": 2,
                            "adapters": {
                                "fixture": {
                                    "command": ["/bin/false"],
                                    "revision": "test:fixture:2",
                                }
                            },
                        }
                    ),
                    encoding="utf-8",
                )
                prepared = await client.call_tool(
                    "prepare_overview_capture",
                    {
                        "overview_path": str(self.overview), "captured_on": "2026-08-06",
                        "segments": [{"segment_id": "label", "region": {"x": 2, "y": 1, "width": 5, "height": 4}}],
                        "source_ref": "MCP overview photo",
                        "adapter_name": "fixture",
                    },
                )
                self.assertFalse(prepared.is_error, prepared.content)
                payload = prepared.structured_content
                capture_id = payload["capture"]["capture_session_id"]
                status = await client.call_tool("capture_status", {"capture_session_id": capture_id})
                self.assertFalse(status.is_error, status.content)
                self.assertEqual(status.structured_content["status"], "awaiting_review")
                reviewed = await client.call_tool(
                    "review_overview_capture",
                    {
                        "capture_session_id": capture_id,
                        "artifact_sha256": payload["capture"]["artifact_sha256"],
                        "links": {},
                    },
                )
                self.assertFalse(reviewed.is_error, reviewed.content)
                status = await client.call_tool(
                    "capture_status", {"capture_session_id": capture_id}
                )
                self.assertEqual(status.structured_content["status"], "prepared")

        asyncio.run(scenario())

    def test_preparation_digest_rejects_observation_or_source_tampering_before_review(self) -> None:
        for mutation in ("observations", "source"):
            with self.subTest(mutation=mutation):
                prepared = self.cli(
                    "capture-prepare",
                    "--overview",
                    str(self.overview),
                    "--captured-on",
                    "2026-08-06",
                    "--segments",
                    '[{"segment_id":"label","region":{"x":2,"y":1,"width":5,"height":4}}]',
                    "--source-ref",
                    f"Tamper test {mutation}",
                    "--adapter-name",
                    "fixture",
                )
                capture = prepared["capture"]
                staging = self.runtime / "capture-staging" / capture["capture_session_id"]
                artifact_path = staging / "artifact.json"
                artifact = json.loads(artifact_path.read_text(encoding="utf-8"))
                if mutation == "observations":
                    artifact["observations"].reverse()
                else:
                    artifact["source"]["original_name"] = "substituted.jpg"
                artifact_path.write_text(
                    json.dumps(
                        artifact,
                        ensure_ascii=False,
                        sort_keys=True,
                        separators=(",", ":"),
                    ),
                    encoding="utf-8",
                )
                failed = self.failed(
                    "capture-review",
                    capture["capture_session_id"],
                    "--artifact-sha256",
                    capture["artifact_sha256"],
                    "--links",
                    "{}",
                )
                self.assertIn("content-addressed session", failed["error"])
                self.assertFalse((staging / "review.json").exists())

    def test_quarantined_request_binding_never_blocks_unrelated_capture(self) -> None:
        before = self.snapshot()
        for entry_kind in ("malformed", "symlink", "fifo"):
            with self.subTest(entry_kind=entry_kind):
                prepared = self.cli(
                    "capture-prepare",
                    "--overview",
                    str(self.overview),
                    "--captured-on",
                    "2026-08-06",
                    "--segments",
                    '[{"segment_id":"label","region":{"x":2,"y":1,"width":5,"height":4}}]',
                    "--source-ref",
                    f"Quarantined request binding {entry_kind}",
                )
                capture_id = prepared["capture"]["capture_session_id"]
                binding = (
                    self.runtime
                    / "capture-staging"
                    / capture_id
                    / "request.sha256"
                )
                binding.unlink()
                if entry_kind == "malformed":
                    binding.write_bytes(b"not-a-request-digest\n")
                elif entry_kind == "symlink":
                    binding.symlink_to(self.overview)
                else:
                    os.mkfifo(binding)

                unrelated = self.cli(
                    "capture-prepare",
                    "--overview",
                    str(self.overview),
                    "--captured-on",
                    "2026-08-06",
                    "--segments",
                    '[{"segment_id":"label","region":{"x":2,"y":1,"width":5,"height":4}}]',
                    "--source-ref",
                    f"Unrelated after quarantined {entry_kind}",
                )
                self.assertNotEqual(
                    unrelated["capture"]["capture_session_id"], capture_id
                )
                status = self.failed("capture-status", capture_id)
                self.assertIn("capture", status["error"])
                self.assertEqual(before, self.snapshot())

    def test_recomputed_artifact_digest_cannot_bypass_content_addressed_session(self) -> None:
        prepared = self.cli(
            "capture-prepare",
            "--overview",
            str(self.overview),
            "--captured-on",
            "2026-08-06",
            "--segments",
            '[{"segment_id":"label","region":{"x":2,"y":1,"width":5,"height":4}}]',
            "--source-ref",
            "Content-address tamper test",
            "--adapter-name",
            "fixture",
        )
        capture = prepared["capture"]
        staging = self.runtime / "capture-staging" / capture["capture_session_id"]
        artifact_path = staging / "artifact.json"
        artifact = json.loads(artifact_path.read_text(encoding="utf-8"))
        artifact["observations"][0]["payload"]["text"] = "attacker replacement"
        artifact_path.write_text(
            json.dumps(
                artifact,
                ensure_ascii=False,
                sort_keys=True,
                separators=(",", ":"),
            ),
            encoding="utf-8",
        )
        replacement_digest = hashlib.sha256(artifact_path.read_bytes()).hexdigest()
        before = self.snapshot()
        failed = self.failed(
            "capture-review",
            capture["capture_session_id"],
            "--artifact-sha256",
            replacement_digest,
            "--links",
            "{}",
        )
        self.assertIn("content-addressed session", failed["error"])
        self.assertEqual(before, self.snapshot())
        self.assertFalse((staging / "review.json").exists())

    def test_capture_review_retry_never_follows_or_blocks_on_proposal_input(self) -> None:
        prepared = self.prepare()
        capture = prepared["capture"]
        proposal_path = (
            self.runtime
            / "proposals"
            / f"{prepared['proposal']['proposal_id']}.json"
        )
        original = proposal_path.read_bytes()
        before = self.snapshot()
        for entry_kind in ("symlink", "fifo", "oversize"):
            with self.subTest(entry_kind=entry_kind):
                proposal_path.unlink()
                if entry_kind == "symlink":
                    proposal_path.symlink_to(self.overview)
                elif entry_kind == "fifo":
                    os.mkfifo(proposal_path)
                else:
                    with proposal_path.open("wb") as handle:
                        handle.truncate(8 * 1024 * 1024 + 1)
                completed = subprocess.run(
                    self.command(
                        "capture-review",
                        capture["capture_session_id"],
                        "--artifact-sha256",
                        capture["artifact_sha256"],
                        "--links",
                        "{}",
                    ),
                    text=True,
                    capture_output=True,
                    check=False,
                    timeout=2,
                )
                self.assertNotEqual(completed.returncode, 0)
                error = json.loads(completed.stderr)["error"]
                self.assertTrue(
                    "regular file" in error
                    or "too large" in error
                    or "symlink" in error,
                    error,
                )
                proposal_path.unlink()
                proposal_path.write_bytes(original)
                self.assertEqual(before, self.snapshot())

    def test_review_rejects_a_store_changed_after_candidate_ranking(self) -> None:
        prepared = self.cli(
            "capture-prepare",
            "--overview",
            str(self.overview),
            "--captured-on",
            "2026-08-06",
            "--segments",
            '[{"segment_id":"label","region":{"x":2,"y":1,"width":5,"height":4}}]',
            "--source-ref",
            "Candidate snapshot test",
            "--adapter-name",
            "fixture",
        )
        self.cli(
            "order",
            "--actor",
            "Capture test",
            "--source-ref",
            "New exact candidate",
            "--name",
            "Model AB-1",
            "--category",
            "tool",
            "--ordered-on",
            "2026-08-06",
            "--order-placed",
        )
        capture = prepared["capture"]
        failed = self.failed(
            "capture-review",
            capture["capture_session_id"],
            "--artifact-sha256",
            capture["artifact_sha256"],
            "--links",
            "{}",
        )
        self.assertIn("inventory changed after capture preparation", failed["error"])
        staging = self.runtime / "capture-staging" / capture["capture_session_id"]
        self.assertFalse((staging / "review.json").exists())

    def test_exif_rotated_jpeg_has_one_declared_coordinate_space(self) -> None:
        overview = self.scratch / "orientation-6.jpg"
        image = Image.new("RGB", (4, 2), (10, 20, 30))
        exif = Image.Exif()
        exif[274] = 6
        image.save(overview, format="JPEG", exif=exif)
        prepared = self.cli(
            "capture-prepare",
            "--overview",
            str(overview),
            "--captured-on",
            "2026-08-06",
            "--segments",
            '[{"segment_id":"bottom","region":{"x":0,"y":3,"width":2,"height":1}}]',
            "--source-ref",
            "EXIF orientation test",
            "--adapter-name",
            "orientation",
        )
        capture = prepared["capture"]
        staging = self.runtime / "capture-staging" / capture["capture_session_id"]
        artifact = json.loads((staging / "artifact.json").read_text(encoding="utf-8"))
        self.assertEqual(artifact["source"]["coordinate_space"], "exif_transposed_pixels")
        self.assertEqual(
            (artifact["source"]["image_width"], artifact["source"]["image_height"]),
            (2, 4),
        )
        self.assertEqual(
            capture["observations"][0]["region"],
            {"x": 0, "y": 0, "width": 2, "height": 4},
        )
        with Image.open(staging / "overview") as exact_source:
            self.assertEqual(exact_source.size, (4, 2))
        with Image.open(staging / artifact["crops"][0]["file"]) as crop:
            self.assertEqual(crop.size, (2, 1))

    def test_review_rejects_unusable_sensitivity_without_sealing(self) -> None:
        item_id = self.cli(
            "order",
            "--actor",
            "Capture test",
            "--source-ref",
            "High sensitivity item",
            "--name",
            "Private object",
            "--category",
            "tool",
            "--ordered-on",
            "2026-08-06",
            "--order-placed",
            "--sensitivity",
            "high",
        )["result"]["item_id"]
        prepared = self.cli(
            "capture-prepare",
            "--overview",
            str(self.overview),
            "--captured-on",
            "2026-08-06",
            "--segments",
            '[{"segment_id":"label","region":{"x":2,"y":1,"width":5,"height":4}}]',
            "--source-ref",
            "Personal evidence",
            "--adapter-name",
            "fixture",
        )
        capture = prepared["capture"]
        failed = self.failed(
            "capture-review",
            capture["capture_session_id"],
            "--artifact-sha256",
            capture["artifact_sha256"],
            "--links",
            json.dumps({"observation-1": item_id}),
        )
        self.assertIn("sensitivity exceeds", failed["error"])
        staging = self.runtime / "capture-staging" / capture["capture_session_id"]
        self.assertFalse((staging / "review.json").exists())

    def test_capture_never_raises_shared_existing_evidence_and_dependents(self) -> None:
        low = self.cli(
            "order",
            "--actor",
            "Capture test",
            "--source-ref",
            "Low evidence source",
            "--name",
            "Low item",
            "--category",
            "test",
            "--ordered-on",
            "2026-08-06",
            "--order-placed",
            "--sensitivity",
            "low",
        )["result"]
        self.cli(
            "add-alias",
            "--item-id",
            low["item_id"],
            "--alias",
            "Legacy low alias",
            "--alias-kind",
            "label",
            "--evidence-id",
            low["evidence_id"],
            "--sensitivity",
            "personal",
        )
        failed = self.failed(
            "capture-prepare",
            "--overview",
            str(self.overview),
            "--captured-on",
            "2026-08-06",
            "--segments",
            '[{"segment_id":"label","region":{"x":2,"y":1,"width":5,"height":4}}]',
            "--evidence-id",
            low["evidence_id"],
            "--sensitivity",
            "personal",
            "--adapter-name",
            "fixture",
        )
        self.assertIn("existing capture evidence type and date must match", failed["error"])
        evidence_rows = [
            json.loads(line)
            for line in (self.store / "evidence.jsonl")
            .read_text(encoding="utf-8")
            .splitlines()
        ]
        evidence = next(
            row for row in evidence_rows if row["evidence_id"] == low["evidence_id"]
        )
        self.assertEqual(evidence["sensitivity"], "personal")
        aliases = (self.store / "aliases.jsonl").read_text(encoding="utf-8")
        self.assertIn("Legacy low alias", aliases)

    def test_existing_capture_evidence_type_and_date_are_canonical(self) -> None:
        self.cli(
            "add-location", "--location-id", "loc-capture", "--name", "Capture shelf",
            "--kind", "room", "--sensitivity", "low",
        )
        ordered = self.cli(
            "order",
            "--actor",
            "Capture test",
            "--source-ref",
            "Evidence contract order",
            "--name",
            "Evidence contract item",
            "--category",
            "test",
            "--ordered-on",
            "2026-08-06",
            "--order-placed",
        )["result"]
        before = self.snapshot()
        wrong_type = self.failed(
            "capture-prepare",
            "--overview",
            str(self.overview),
            "--captured-on",
            "2026-08-06",
            "--segments",
            '[{"segment_id":"label","region":{"x":2,"y":1,"width":5,"height":4}}]',
            "--evidence-id",
            ordered["evidence_id"],
        )
        self.assertIn("type and date must match", wrong_type["error"])
        received = self.cli(
            "receive",
            "--actor",
            "Capture test",
            "--source-ref",
            "Evidence contract delivery",
            "--item-id",
            ordered["item_id"],
            "--received-on",
            "2026-08-06",
            "--location-id",
            "loc-capture",
        )["result"]
        before_date_failure = self.snapshot()
        wrong_date = self.failed(
            "capture-prepare",
            "--overview",
            str(self.overview),
            "--captured-on",
            "2026-08-07",
            "--segments",
            '[{"segment_id":"label","region":{"x":2,"y":1,"width":5,"height":4}}]',
            "--evidence-id",
            received["evidence_id"],
            "--evidence-type",
            "user_source",
        )
        self.assertIn("passive capture preparation cannot reuse", wrong_date["error"])
        self.assertNotEqual(before, before_date_failure)
        self.assertEqual(before_date_failure, self.snapshot())
        self.assertFalse((self.runtime / "capture-staging").exists())

    def test_capture_cannot_spread_state_bearing_evidence_to_another_item(self) -> None:
        self.cli(
            "add-location", "--location-id", "loc-capture", "--name", "Capture shelf",
            "--kind", "room", "--sensitivity", "low",
        )
        first = self.cli(
            "order",
            "--actor",
            "Capture test",
            "--source-ref",
            "First item order",
            "--name",
            "Physically checked source item",
            "--category",
            "test",
            "--ordered-on",
            "2026-08-06",
            "--order-placed",
        )["result"]
        explicit_current = self.cli(
            "receive",
            "--actor",
            "Capture test",
            "--source-ref",
            "First item delivery check",
            "--item-id",
            first["item_id"],
            "--received-on",
            "2026-08-06",
            "--location-id",
            "loc-capture",
        )["result"]
        before = self.snapshot()
        failed = self.failed(
            "capture-prepare",
            "--overview",
            str(self.overview),
            "--captured-on",
            "2026-08-06",
            "--segments",
            '[{"segment_id":"label","region":{"x":2,"y":1,"width":5,"height":4}}]',
            "--evidence-id",
            explicit_current["evidence_id"],
            "--adapter-name",
            "fixture",
        )
        self.assertIn("cannot reuse current-possession", failed["error"])
        self.assertEqual(before, self.snapshot())
        self.assertFalse((self.runtime / "capture-staging").exists())

    def test_identical_crop_bytes_preserve_every_source_region(self) -> None:
        overview = self.scratch / "uniform.png"
        Image.new("RGB", (4, 2), (40, 40, 40)).save(overview)
        prepared = self.cli(
            "capture-prepare",
            "--overview",
            str(overview),
            "--captured-on",
            "2026-08-06",
            "--segments",
            '[{"segment_id":"left","region":{"x":0,"y":0,"width":2,"height":2}},'
            '{"segment_id":"right","region":{"x":2,"y":0,"width":2,"height":2}}]',
            "--source-ref",
            "Two identical uniform regions",
        )
        capture = prepared["capture"]
        reviewed = self.cli(
            "capture-review",
            capture["capture_session_id"],
            "--artifact-sha256",
            capture["artifact_sha256"],
            "--links",
            "{}",
        )
        self.cli("proposal-apply", reviewed["proposal"]["proposal_id"])
        links = [
            json.loads(line)
            for line in (self.store / "evidence_assets.jsonl")
            .read_text(encoding="utf-8")
            .splitlines()
        ]
        crop_links = [row for row in links if row["role"] == "crop"]
        self.assertEqual(len(crop_links), 1)
        self.assertEqual(
            json.loads(crop_links[0]["region_json"]),
            {
                "regions": [
                    {"height": 2, "width": 2, "x": 0, "y": 0},
                    {"height": 2, "width": 2, "x": 2, "y": 0},
                ]
            },
        )

    def test_review_rejects_preexisting_generic_source_or_crop_annotation(self) -> None:
        self.cli(
            "add-location", "--location-id", "loc-capture",
            "--name", "Capture shelf", "--kind", "room", "--sensitivity", "low",
        )
        overview = self.scratch / "annotation-collision.png"
        Image.new("RGB", (4, 2), (50, 50, 50)).save(overview)
        probe = self.cli(
            "capture-prepare",
            "--overview",
            str(overview),
            "--captured-on",
            "2026-08-06",
            "--segments",
            '[{"segment_id":"left","region":{"x":0,"y":0,"width":2,"height":2}}]',
            "--source-ref",
            "Collision probe",
        )
        probe_staging = (
            self.runtime
            / "capture-staging"
            / probe["capture"]["capture_session_id"]
        )
        probe_artifact = json.loads(
            (probe_staging / "artifact.json").read_text(encoding="utf-8")
        )
        crop_file = self.scratch / "exact-generated-crop.png"
        crop_file.write_bytes(
            (probe_staging / probe_artifact["crops"][0]["file"]).read_bytes()
        )

        for role, media_file in (("source", overview), ("crop", crop_file)):
            with self.subTest(role=role):
                item = self.cli(
                    "order",
                    "--actor",
                    "Capture test",
                    "--source-ref",
                    f"Existing {role} annotation",
                    "--name",
                    f"Collision item {role}",
                    "--category",
                    "test",
                    "--ordered-on",
                    "2026-08-06",
                    "--order-placed",
                    "--sensitivity",
                    "personal",
                )["result"]
                evidence_id = self.cli(
                    "record-evidence",
                    "--item-id",
                    item["item_id"],
                    "--source-ref",
                    f"Existing passive {role} source",
                    "--captured-on",
                    "2026-08-06",
                    "--evidence-type",
                    "user_source",
                    "--claim-strength",
                    "research_only",
                    "--sensitivity",
                    "personal",
                )["result"]["evidence_id"]
                self.cli(
                    "attach-media",
                    "--evidence-id",
                    evidence_id,
                    "--file",
                    str(media_file),
                    "--role",
                    role,
                    "--region",
                    '{"page":1}',
                    "--captured-on",
                    "2026-08-06",
                    "--media-type",
                    "image/png",
                    "--sensitivity",
                    "personal",
                )
                prepared = self.cli(
                    "capture-prepare",
                    "--overview",
                    str(overview),
                    "--captured-on",
                    "2026-08-06",
                    "--segments",
                    '[{"segment_id":"left","region":{"x":0,"y":0,"width":2,"height":2}}]',
                    "--evidence-id",
                    evidence_id,
                    "--sensitivity",
                    "personal",
                )
                capture = prepared["capture"]
                failed = self.failed(
                    "capture-review",
                    capture["capture_session_id"],
                    "--artifact-sha256",
                    capture["artifact_sha256"],
                    "--links",
                    "{}",
                )
                self.assertIn("annotation cannot be merged", failed["error"])
                staging = (
                    self.runtime / "capture-staging" / capture["capture_session_id"]
                )
                self.assertFalse((staging / "review.json").exists())

    def test_explicit_model_identifier_ranks_a_candidate_without_text_overlap(self) -> None:
        item_id = self.cli(
            "order",
            "--actor",
            "Capture test",
            "--source-ref",
            "Identifier-only candidate",
            "--name",
            "Unrelated canonical label",
            "--category",
            "test",
            "--identifiers",
            '{"manufacturer_code":"IDENTIFIER-ONLY-42"}',
            "--ordered-on",
            "2026-08-06",
            "--order-placed",
        )["result"]["item_id"]
        prepared = self.cli(
            "capture-prepare",
            "--overview",
            str(self.overview),
            "--captured-on",
            "2026-08-06",
            "--segments",
            '[{"segment_id":"label","region":{"x":2,"y":1,"width":5,"height":4}}]',
            "--source-ref",
            "Model identifier image",
            "--adapter-name",
            "identifier",
        )
        candidates = prepared["capture"]["duplicate_candidates"]["observation-1"]
        self.assertEqual(candidates[0]["item_id"], item_id)
        self.assertIn(
            "exact_model_identifier",
            {entry["kind"] for entry in candidates[0]["evidence"]},
        )

    def test_benchmark_runs_the_checked_synthetic_corpus_without_mutation(self) -> None:
        before = self.snapshot()
        report = self.cli(
            "capture-benchmark",
            "--input",
            str(BENCHMARK),
            "--adapter-name",
            "benchmark",
        )
        self.assertEqual(report["claim"], "synthetic-fixture-only")
        self.assertEqual(report["segmentation_recall"]["value"], 0.0)
        self.assertTrue(report["segmentation_recall"]["errors"])
        self.assertEqual(report["field_exact_match"]["value"], 1.0)
        self.assertEqual(report["barcode_exact_match"]["value"], 1.0)
        self.assertEqual(report["duplicate_top_1"]["value"], 1.0)
        self.assertEqual(before, self.snapshot())

    def test_benchmark_input_rejects_symlink_fifo_and_oversize_without_blocking(self) -> None:
        symlink = self.scratch / "benchmark-link.json"
        symlink.symlink_to(BENCHMARK)
        self.assertIn(
            "regular file",
            self.failed(
                "capture-benchmark",
                "--input",
                str(symlink),
                "--adapter-name",
                "benchmark",
            )["error"],
        )

        fifo = self.scratch / "benchmark.fifo"
        os.mkfifo(fifo)
        completed = subprocess.run(
            self.command(
                "capture-benchmark",
                "--input",
                str(fifo),
                "--adapter-name",
                "benchmark",
            ),
            text=True,
            capture_output=True,
            check=False,
            timeout=2,
        )
        self.assertNotEqual(completed.returncode, 0)
        self.assertIn("regular file", json.loads(completed.stderr)["error"])

        oversized = self.scratch / "oversized-benchmark.json"
        with oversized.open("wb") as handle:
            handle.truncate(64 * 1024 * 1024 + 1)
        self.assertIn(
            "too large",
            self.failed(
                "capture-benchmark",
                "--input",
                str(oversized),
                "--adapter-name",
                "benchmark",
            )["error"],
        )

    def test_capture_materialization_has_no_direct_cli_surface(self) -> None:
        prepared = self.prepare()
        capture_id = prepared["capture"]["capture_session_id"]
        before = self.snapshot()
        attempted = subprocess.run(
            self.command(
                "capture-commit", "--capture-session-id", capture_id,
                "--proposal-materialization",
            ),
            text=True,
            capture_output=True,
            check=False,
        )
        self.assertEqual(attempted.returncode, 1)
        parser_error = json.loads(attempted.stderr)
        self.assertEqual(parser_error["status"], "error")
        self.assertIn("invalid command arguments", parser_error["error"])
        self.assertEqual(before, self.snapshot())
        self.assertEqual(
            (self.store / "proposal_commits.jsonl").read_text(encoding="utf-8"), ""
        )

    def test_applied_capture_status_does_not_depend_on_runtime_staging(self) -> None:
        prepared = self.prepare()
        capture_id = prepared["capture"]["capture_session_id"]
        applied = self.cli("proposal-apply", prepared["proposal"]["proposal_id"])
        staging = self.runtime / "capture-staging" / capture_id
        self.assertEqual(applied["capture_staging_cleanup"], "removed")
        self.assertFalse(staging.exists())
        status = self.cli("capture-status", capture_id)
        self.assertEqual(status["status"], "applied")
        self.assertIsNone(status["staging"])
        self.assertEqual(status["provenance_state"], "bound")
        self.assertEqual(status["source_count"], 1)
        self.assertEqual(status["crop_count"], 1)

    def test_bound_capture_survives_verified_export_and_blank_restore(self) -> None:
        prepared = self.prepare()
        capture_id = prepared["capture"]["capture_session_id"]
        applied = self.cli("proposal-apply", prepared["proposal"]["proposal_id"])
        staging = self.runtime / "capture-staging" / capture_id
        self.assertEqual(applied["capture_staging_cleanup"], "removed")
        self.assertFalse(staging.exists())
        archive = self.scratch / "capture-export.tar.gz"
        self.cli("export", "--output", str(archive))

        restored_root = self.scratch / "restored-inventory"
        restored_runtime = self.scratch / "restored-runtime"
        restored_media = self.scratch / "restored-media"

        def restored_cli(*arguments: str) -> dict:
            command = [
                sys.executable,
                str(CLI),
                "--inventory-root",
                str(restored_root),
                "--runtime-dir",
                str(restored_runtime),
                "--media-root",
                str(restored_media),
                "--capture-adapters-config",
                str(self.adapter_config),
                "--scope",
                "private",
                *arguments,
            ]
            completed = subprocess.run(
                command, text=True, capture_output=True, check=False
            )
            self.assertEqual(
                completed.returncode, 0, completed.stderr or completed.stdout
            )
            return json.loads(completed.stdout)

        restored_cli("restore", "--archive", str(archive))
        restored_status = restored_cli("capture-status", capture_id)
        self.assertEqual(restored_status["status"], "applied")
        self.assertIsNone(restored_status["staging"])
        self.assertEqual(restored_status["provenance_state"], "bound")
        self.assertEqual(restored_cli("status")["verification"]["failures"], [])

    def test_bound_capture_provenance_rejects_every_canonical_tamper(self) -> None:
        item_id = self.cli(
            "order",
            "--actor",
            "Capture fixture",
            "--source-ref",
            "Capture provenance item",
            "--name",
            "Model AB-1",
            "--category",
            "capture fixture",
            "--ordered-on",
            "2026-08-06",
            "--order-placed",
        )["result"]["item_id"]
        staged = self.cli(
            "capture-prepare",
            "--overview",
            str(self.overview),
            "--captured-on",
            "2026-08-06",
            "--segments",
            '[{"segment_id":"label","region":{"x":2,"y":1,"width":5,"height":4}}]',
            "--source-ref",
            "Bound provenance fixture",
            "--adapter-name",
            "fixture",
        )
        capture_id = staged["capture"]["capture_session_id"]
        reviewed = self.cli(
            "capture-review",
            capture_id,
            "--artifact-sha256",
            staged["capture"]["artifact_sha256"],
            "--links",
            json.dumps({"observation-1": item_id}),
        )
        self.cli("proposal-apply", reviewed["proposal"]["proposal_id"])
        self.assertEqual(self.cli("status")["verification"]["failures"], [])

        table_names = (
            "capture_sessions",
            "capture_observations",
            "evidence_assets",
            "evidence",
            "items",
            "media_assets",
            "proposal_commits",
        )
        originals = {
            name: (self.store / f"{name}.jsonl").read_bytes()
            for name in table_names
        }

        def read_rows(name: str) -> list[dict]:
            return [
                json.loads(line)
                for line in (self.store / f"{name}.jsonl").read_text().splitlines()
                if line
            ]

        def write_rows(name: str, rows: list[dict]) -> None:
            (self.store / f"{name}.jsonl").write_text(
                "".join(json.dumps(row, sort_keys=True) + "\n" for row in rows),
                encoding="utf-8",
            )

        for mutation in (
            "artifact_hash",
            "artifact_wire",
            "review_hash",
            "review_links",
            "observation_payload",
            "observation_evidence",
            "observation_index",
            "crop_region",
            "media_sensitivity",
            "session_sensitivity",
            "duplicate_session",
            "evidence_binding",
            "proposal_receipt_missing",
            "proposal_receipt_duplicate",
            "proposal_receipt_digest",
        ):
            with self.subTest(mutation=mutation):
                for name, payload in originals.items():
                    (self.store / f"{name}.jsonl").write_bytes(payload)
                sessions = read_rows("capture_sessions")
                session = sessions[0]
                artifact = json.loads(session["artifact_json"])
                review = json.loads(session["review_json"])
                if mutation == "artifact_hash":
                    session["artifact_sha256"] = "0" * 64
                    write_rows("capture_sessions", sessions)
                elif mutation == "artifact_wire":
                    artifact["source"]["original_name"] = "substituted.png"
                    artifact_wire = json.dumps(
                        artifact,
                        ensure_ascii=False,
                        sort_keys=True,
                        separators=(",", ":"),
                    )
                    session["artifact_json"] = artifact_wire
                    session["artifact_sha256"] = hashlib.sha256(
                        artifact_wire.encode()
                    ).hexdigest()
                    write_rows("capture_sessions", sessions)
                elif mutation == "review_hash":
                    session["review_sha256"] = "0" * 64
                    write_rows("capture_sessions", sessions)
                elif mutation == "review_links":
                    review["links"] = {"observation-1": "item-missing"}
                    review_wire = (
                        json.dumps(
                            review,
                            ensure_ascii=False,
                            indent=2,
                            sort_keys=True,
                        )
                        + "\n"
                    )
                    session["review_json"] = review_wire
                    session["review_sha256"] = hashlib.sha256(
                        review_wire.encode()
                    ).hexdigest()
                    write_rows("capture_sessions", sessions)
                elif mutation == "observation_payload":
                    observations = read_rows("capture_observations")
                    value = json.loads(observations[0]["observation_json"])
                    value["payload"]["text"] = "tampered OCR"
                    observations[0]["observation_json"] = json.dumps(
                        value, ensure_ascii=False, sort_keys=True, separators=(",", ":")
                    )
                    write_rows("capture_observations", observations)
                elif mutation == "observation_evidence":
                    observations = read_rows("capture_observations")
                    observations[0]["evidence_id"] = "ev-missing"
                    write_rows("capture_observations", observations)
                elif mutation == "observation_index":
                    observations = read_rows("capture_observations")
                    observations[0]["observation_index"] = 99
                    write_rows("capture_observations", observations)
                elif mutation == "crop_region":
                    links = read_rows("evidence_assets")
                    crop_link = next(link for link in links if link["role"] == "crop")
                    crop_link["region_json"] = (
                        '{"height":1,"width":1,"x":0,"y":0}'
                    )
                    write_rows("evidence_assets", links)
                elif mutation == "media_sensitivity":
                    assets = read_rows("media_assets")
                    source_digest = artifact["source"]["sha256"]
                    next(asset for asset in assets if asset["sha256"] == source_digest)[
                        "sensitivity"
                    ] = "low"
                    write_rows("media_assets", assets)
                elif mutation == "session_sensitivity":
                    session["sensitivity"] = "low"
                    write_rows("capture_sessions", sessions)
                elif mutation == "duplicate_session":
                    write_rows("capture_sessions", [*sessions, dict(session)])
                elif mutation == "evidence_binding":
                    evidence_rows = read_rows("evidence")
                    evidence = next(
                        row
                        for row in evidence_rows
                        if row["evidence_id"] == session["evidence_id"]
                    )
                    evidence["captured_on"] = "2026-08-05"
                    write_rows("evidence", evidence_rows)
                elif mutation == "proposal_receipt_missing":
                    write_rows("proposal_commits", [])
                elif mutation == "proposal_receipt_duplicate":
                    receipts = read_rows("proposal_commits")
                    write_rows("proposal_commits", [*receipts, dict(receipts[0])])
                else:
                    receipts = read_rows("proposal_commits")
                    receipts[0]["operations_digest"] = "0" * 64
                    write_rows("proposal_commits", receipts)

                database_before = (self.runtime / "inventory.sqlite").read_bytes()
                failure = self.failed("status")
                self.assertIn("capture provenance", failure["error"])
                capture_failure = self.failed("capture-status", capture_id)
                self.assertTrue(
                    "capture provenance" in capture_failure["error"]
                    or "canonical generation changed" in capture_failure["error"]
                )
                self.assertEqual(
                    (self.runtime / "inventory.sqlite").read_bytes(), database_before
                )

        for name, payload in originals.items():
            (self.store / f"{name}.jsonl").write_bytes(payload)
        self.assertEqual(self.cli("status")["verification"]["failures"], [])

    def test_capture_sensitivity_follows_candidates_and_reused_media(self) -> None:
        self.cli(
            "order",
            "--actor",
            "Capture fixture",
            "--source-ref",
            "High candidate fixture",
            "--name",
            "Model AB-1",
            "--category",
            "capture fixture",
            "--ordered-on",
            "2026-08-06",
            "--order-placed",
            "--sensitivity",
            "high",
        )
        first = self.cli(
            "capture-prepare",
            "--overview",
            str(self.overview),
            "--captured-on",
            "2026-08-06",
            "--segments",
            '[{"segment_id":"label","region":{"x":2,"y":1,"width":5,"height":4}}]',
            "--source-ref",
            "High candidate capture",
            "--adapter-name",
            "fixture",
            "--sensitivity",
            "low",
        )
        first_id = first["capture"]["capture_session_id"]
        first_artifact = json.loads(
            (
                self.runtime
                / "capture-staging"
                / first_id
                / "artifact.json"
            ).read_text()
        )
        self.assertEqual(first_artifact["sensitivity"], "high")
        reviewed = self.cli(
            "capture-review",
            first_id,
            "--artifact-sha256",
            first["capture"]["artifact_sha256"],
            "--links",
            "{}",
        )
        self.cli("proposal-apply", reviewed["proposal"]["proposal_id"])

        second = self.cli(
            "capture-prepare",
            "--overview",
            str(self.overview),
            "--captured-on",
            "2026-08-07",
            "--segments",
            '[{"segment_id":"label","region":{"x":2,"y":1,"width":5,"height":4}}]',
            "--source-ref",
            "Nominally low reuse",
            "--sensitivity",
            "low",
        )
        second_review = self.cli(
            "capture-review",
            second["capture"]["capture_session_id"],
            "--artifact-sha256",
            second["capture"]["artifact_sha256"],
            "--links",
            "{}",
        )
        self.cli("proposal-apply", second_review["proposal"]["proposal_id"])
        sessions = [
            json.loads(line)
            for line in (self.store / "capture_sessions.jsonl").read_text().splitlines()
        ]
        self.assertEqual([session["sensitivity"] for session in sessions], ["high", "high"])
        assets = [
            json.loads(line)
            for line in (self.store / "media_assets.jsonl").read_text().splitlines()
        ]
        self.assertTrue(assets)
        self.assertEqual({asset["sensitivity"] for asset in assets}, {"high"})
        self.assertEqual(self.cli("status")["verification"]["failures"], [])


if __name__ == "__main__":
    unittest.main()
