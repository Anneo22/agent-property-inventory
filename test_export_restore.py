#!/usr/bin/env python3
"""Content-addressed media and portable restore acceptance tests."""

from __future__ import annotations

import hashlib
import io
import json
import os
import subprocess
import sys
import tarfile
import tempfile
import unittest
from pathlib import Path

HERE = Path(__file__).resolve().parent
CLI = HERE / "property_inventory.py"


class ExportRestoreTest(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory(prefix="inventory-export-restore-")
        self.scratch = Path(self.temp.name)
        self.root = self.scratch / "inventory"
        self.runtime = self.scratch / "runtime"
        self.media = self.scratch / "media"
        self.assertEqual(self.cli("init")["status"], "initialized")

    def tearDown(self) -> None:
        self.temp.cleanup()

    def command_for(
        self,
        root: Path,
        runtime: Path,
        media: Path,
        *arguments: str,
    ) -> list[str]:
        return [
            sys.executable,
            str(CLI),
            "--inventory-root",
            str(root),
            "--runtime-dir",
            str(runtime),
            "--media-root",
            str(media),
            *arguments,
        ]

    def command(self, *arguments: str) -> list[str]:
        return self.command_for(
            self.root, self.runtime, self.media, *arguments
        )

    def execute(self, command: list[str], *, succeeds: bool = True) -> dict:
        completed = subprocess.run(command, text=True, capture_output=True, check=False)
        if succeeds:
            self.assertEqual(completed.returncode, 0, completed.stderr or completed.stdout)
            return json.loads(completed.stdout)
        self.assertNotEqual(completed.returncode, 0, completed.stdout)
        return json.loads(completed.stderr)

    def cli(self, *arguments: str) -> dict:
        return self.execute(self.command(*arguments))

    def test_export_to_blank_restore_preserves_store_identity_and_media(self) -> None:
        self.cli(
            "add-location", "--name", "Export fixture location", "--location-id", "loc-export",
            "--kind", "room",
        )
        item_id = self.cli(
            "order",
            "--actor",
            "Export fixture",
            "--source-ref",
            "Export order fixture",
            "--name",
            "export-media-object",
            "--category",
            "test fixture",
            "--ordered-on",
            "2026-08-05",
            "--order-placed",
            "--sensitivity",
            "high",
        )["result"]["item_id"]
        evidence_id = self.cli(
            "receive",
            "--actor",
            "Export fixture",
            "--source-ref",
            "Export physical fixture",
            "--item-id",
            item_id,
            "--received-on",
            "2026-08-06",
            "--location-id",
            "loc-export",
            "--physical-check",
        )["result"]["evidence_id"]
        source = self.scratch / "fixture.jpg"
        payload = b"physical evidence fixture bytes\x00\xff"
        source.write_bytes(payload)
        attached = self.cli(
            "attach-media",
            "--evidence-id",
            evidence_id,
            "--file",
            str(source),
            "--role",
            "source",
            "--captured-on",
            "2026-08-06",
            "--media-type",
            "application/octet-stream",
            "--sensitivity",
            "low",
        )
        digest = attached["result"]["sha256"]
        self.assertEqual(attached["result"]["sensitivity"], "high")
        media_path = self.media / "sha256" / digest[:2] / digest
        self.assertEqual(media_path.read_bytes(), payload)
        assets = [
            json.loads(line)
            for line in (self.root / "Data" / "store" / "media_assets.jsonl")
            .read_text()
            .splitlines()
        ]
        self.assertEqual(assets[0]["sensitivity"], "high")
        self.assertEqual(self.cli("status")["verification"]["failures"], [])

        archive = self.scratch / "inventory.tar.gz"
        exported = self.cli("export", "--output", str(archive))
        self.assertEqual(exported["status"], "exported")
        self.assertEqual(exported["media_assets"], 1)
        second_archive = self.scratch / "inventory-second.tar.gz"
        self.cli("export", "--output", str(second_archive))
        self.assertEqual(second_archive.read_bytes(), archive.read_bytes())
        source_store = {
            path.name: path.read_bytes()
            for path in sorted((self.root / "Data" / "store").glob("*.jsonl"))
        }
        source_identity = json.loads(source_store["metadata.jsonl"])["inventory_id"]

        restored_root = self.scratch / "restored-inventory"
        restored_runtime = self.scratch / "restored-runtime"
        restored_media = self.scratch / "restored-media"
        restored = self.execute(
            self.command_for(
                restored_root,
                restored_runtime,
                restored_media,
                "restore",
                "--archive",
                str(archive),
            )
        )
        self.assertEqual(restored["status"], "restored")
        self.assertEqual(restored["inventory_id"], source_identity)
        restored_store = {
            path.name: path.read_bytes()
            for path in sorted((restored_root / "Data" / "store").glob("*.jsonl"))
        }
        self.assertEqual(restored_store, source_store)
        self.assertEqual(
            (restored_media / "sha256" / digest[:2] / digest).read_bytes(), payload
        )
        status = self.execute(
            self.command_for(restored_root, restored_runtime, restored_media, "status")
        )
        self.assertEqual(status["verification"]["failures"], [])
        self.assertEqual(status["foreign_key_failures"], 0)

    def test_restore_rejects_unsafe_archive_and_nonempty_target(self) -> None:
        unsafe = self.scratch / "unsafe.tar.gz"
        with tarfile.open(unsafe, "w:gz") as archive:
            info = tarfile.TarInfo("../escape")
            payload = b"escape"
            info.size = len(payload)
            archive.addfile(info, io.BytesIO(payload))
        target = self.scratch / "unsafe-target"
        target_media = self.scratch / "unsafe-media"
        failure = self.execute(
            self.command_for(
                target,
                self.scratch / "unsafe-runtime",
                target_media,
                "restore",
                "--archive",
                str(unsafe),
            ),
            succeeds=False,
        )
        self.assertIn("unsafe or duplicate export member", failure["error"])
        self.assertFalse((self.scratch / "escape").exists())

        target.mkdir()
        (target / "keep.txt").write_text("keep")
        nonempty = self.execute(
            self.command_for(
                target,
                self.scratch / "nonempty-runtime",
                target_media,
                "restore",
                "--archive",
                str(unsafe),
            ),
            succeeds=False,
        )
        self.assertIn("restore target is not empty", nonempty["error"])
        self.assertEqual((target / "keep.txt").read_text(), "keep")

    def test_failed_attachment_removes_new_content_bytes(self) -> None:
        evidence_id = self.cli(
            "order",
            "--actor",
            "Media failure fixture",
            "--source-ref",
            "Media failure order",
            "--name",
            "media-failure-object",
            "--category",
            "test fixture",
            "--ordered-on",
            "2026-08-05",
            "--order-placed",
        )["result"]["evidence_id"]
        source = self.scratch / "orphan.jpg"
        payload = b"must not remain orphaned"
        source.write_bytes(payload)
        digest = hashlib.sha256(payload).hexdigest()
        environment = dict(os.environ)
        environment["PROPERTY_INVENTORY_FAIL_AFTER_MEDIA_INSTALL"] = "1"
        completed = subprocess.run(
            self.command(
                "attach-media",
                "--evidence-id",
                evidence_id,
                "--file",
                str(source),
                "--role",
                "source",
                "--media-type",
                "application/octet-stream",
            ),
            text=True,
            capture_output=True,
            check=False,
            env=environment,
        )
        self.assertNotEqual(completed.returncode, 0)
        self.assertIn("injected failure after media install", completed.stderr)
        self.assertFalse((self.media / "sha256" / digest[:2] / digest).exists())
        self.assertEqual(
            (self.root / "Data" / "store" / "media_assets.jsonl").read_text(), ""
        )

    def test_attach_media_requires_a_durable_explicit_root(self) -> None:
        source = self.scratch / "explicit-root.jpg"
        source.write_bytes(b"explicit root")
        command = [
            sys.executable,
            str(CLI),
            "--inventory-root",
            str(self.root),
            "--runtime-dir",
            str(self.runtime),
            "attach-media",
            "--evidence-id",
            "ev-anything",
            "--file",
            str(source),
            "--role",
            "source",
        ]
        failure = self.execute(command, succeeds=False)
        self.assertIn("attach-media requires --media-root", failure["error"])
        self.assertFalse((self.runtime / "media").exists())

    def test_post_commit_error_never_deletes_referenced_media(self) -> None:
        evidence_id = self.cli(
            "order",
            "--actor",
            "Post-commit fixture",
            "--source-ref",
            "Post-commit order",
            "--name",
            "post-commit-media-object",
            "--category",
            "test fixture",
            "--ordered-on",
            "2026-08-05",
            "--order-placed",
        )["result"]["evidence_id"]
        source = self.scratch / "post-commit.jpg"
        payload = b"must survive a post-commit exception"
        source.write_bytes(payload)
        digest = hashlib.sha256(payload).hexdigest()
        environment = dict(os.environ)
        environment["PROPERTY_INVENTORY_RAISE_AFTER_COMMIT"] = "1"
        completed = subprocess.run(
            self.command(
                "attach-media",
                "--evidence-id",
                evidence_id,
                "--file",
                str(source),
                "--role",
                "source",
                "--media-type",
                "application/octet-stream",
            ),
            text=True,
            capture_output=True,
            check=False,
            env=environment,
        )
        self.assertNotEqual(completed.returncode, 0)
        self.assertIn("injected failure after canonical commit", completed.stderr)
        media_path = self.media / "sha256" / digest[:2] / digest
        self.assertEqual(media_path.read_bytes(), payload)
        assets = [
            json.loads(line)
            for line in (self.root / "Data" / "store" / "media_assets.jsonl")
            .read_text()
            .splitlines()
        ]
        self.assertEqual([asset["sha256"] for asset in assets], [digest])
        self.assertEqual(self.cli("status")["verification"]["failures"], [])


if __name__ == "__main__":
    unittest.main()
