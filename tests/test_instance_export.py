#!/usr/bin/env python3
"""Instance-root and auxiliary export acceptance tests."""

from __future__ import annotations

import hashlib
import io
import json
import os
import shutil
import subprocess
import sys
import tarfile
import tempfile
import threading
import unittest
from pathlib import Path
from unittest.mock import patch

HERE = Path(__file__).resolve().parents[1]
CLI = HERE / "property_inventory.py"
sys.path.insert(0, str(HERE / "src"))
from property_inventory.cli import (  # noqa: E402
    data_paths,
    file_digest,
    restore_transfer_build,
    restore_transfer_stage,
)
from property_inventory.cli import execute as execute_in_process  # noqa: E402


class InstanceExportTest(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory(prefix="inventory-instance-export-")
        self.scratch = Path(self.temp.name)
        self.root = self.scratch / "inventory"
        self.runtime = self.scratch / "runtime"
        self.media = self.scratch / "media"
        self.catalogue = self.scratch / "catalogue" / "Inventory.md"

    def tearDown(self) -> None:
        self.temp.cleanup()

    def command_for(
        self,
        root: Path,
        runtime: Path,
        media: Path,
        catalogue: Path,
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
            "--catalogue-output",
            str(catalogue),
            *arguments,
        ]

    def command(self, *arguments: str) -> list[str]:
        return self.command_for(
            self.root,
            self.runtime,
            self.media,
            self.catalogue,
            *arguments,
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

    def initialize(self) -> dict:
        return self.cli("init")

    @staticmethod
    def digest(payload: bytes) -> str:
        return hashlib.sha256(payload).hexdigest()

    def write_auxiliary_fixture(self) -> dict[str, bytes]:
        data = self.root / "Data"
        declared = {
            "verification_policy.json": b'{"acceptance_minimums":{},"state_overrides":{}}\n',
            "source-inventory.json": b"[]\n",
            "account-candidates.json": b"[]\n",
            "source/raw/cold-source.bin": b"cold source bytes\x00\xff\n",
        }
        for relative, payload in declared.items():
            destination = data / relative
            destination.parent.mkdir(parents=True, exist_ok=True)
            destination.write_bytes(payload)
        written = self.cli(
            "auxiliary-manifest",
            "--include",
            "source/raw/cold-source.bin",
        )
        self.assertEqual(written["status"], "written")
        self.assertEqual(written["checks"]["verification"]["failures"], [])
        manifest_path = data / "auxiliary-manifest.json"
        manifest_payload = manifest_path.read_bytes()
        self.assertEqual(
            json.loads(manifest_payload),
            {
                "format": 1,
                "files": {
                    relative: {"sha256": self.digest(payload)}
                    for relative, payload in declared.items()
                },
            },
        )
        return {"auxiliary-manifest.json": manifest_payload, **declared}

    def test_catalogue_output_is_independent_of_the_inventory_root(self) -> None:
        initialized = self.initialize()

        self.assertEqual(initialized["status"], "initialized")
        self.assertTrue(self.catalogue.is_file())
        self.assertFalse((self.root / "Inventory.md").exists())

    def test_catalogue_output_symlink_cannot_overwrite_external_bytes(self) -> None:
        self.initialize()
        sentinel = self.scratch / "external-catalogue-sentinel.md"
        payload = b"external catalogue sentinel\n"
        sentinel.write_bytes(payload)
        catalogue_link = self.scratch / "catalogue-link.md"
        catalogue_link.symlink_to(sentinel)

        failed = self.execute(
            self.command_for(
                self.root,
                self.runtime,
                self.media,
                catalogue_link,
                "status",
            ),
            succeeds=False,
        )

        self.assertIn("catalogue_output must not be a symlink", failed["error"])
        self.assertEqual(sentinel.read_bytes(), payload)

    def test_runtime_and_catalogue_cannot_be_shared_between_instances(self) -> None:
        self.initialize()
        first_catalogue = self.catalogue.read_bytes()

        second_root = self.scratch / "second-inventory"
        shared_runtime_failed = self.execute(
            self.command_for(
                second_root,
                self.runtime,
                self.scratch / "second-media",
                self.catalogue,
                "init",
            ),
            succeeds=False,
        )
        self.assertIn("runtime", shared_runtime_failed["error"])
        self.assertFalse(second_root.exists())
        self.assertEqual(self.catalogue.read_bytes(), first_catalogue)

        shared_catalogue_failed = self.execute(
            self.command_for(
                second_root,
                self.scratch / "second-runtime",
                self.scratch / "second-media",
                self.catalogue,
                "init",
            ),
            succeeds=False,
        )
        self.assertIn("catalogue", shared_catalogue_failed["error"])
        self.assertFalse(second_root.exists())
        self.assertEqual(self.catalogue.read_bytes(), first_catalogue)
        self.assertEqual(self.cli("status")["verification"]["failures"], [])

    def test_init_failure_rolls_back_root_runtime_and_catalogue(self) -> None:
        blocked_parent = self.scratch / "blocked-init-catalogue-parent"
        blocked_parent.write_bytes(b"preserve blocked parent")
        root = self.scratch / "failed-init-root"
        runtime = self.scratch / "failed-init-runtime"
        media = self.scratch / "failed-init-media"

        failed = self.execute(
            self.command_for(
                root,
                runtime,
                media,
                blocked_parent / "Inventory.md",
                "init",
            ),
            succeeds=False,
        )

        self.assertIn("blocked-init-catalogue-parent", failed["error"])
        self.assertFalse(root.exists())
        self.assertFalse(runtime.exists())
        self.assertEqual(blocked_parent.read_bytes(), b"preserve blocked parent")

    def test_export_rejects_symlink_and_forbidden_output_aliases(self) -> None:
        self.initialize()
        forbidden = self.scratch / "forbidden-export"
        forbidden.mkdir()
        escaped = forbidden / "escaped.tar.gz"
        alias = self.scratch / "export-alias.tar.gz"
        alias.symlink_to(escaped)

        failed = self.execute(
            self.command(
                "--forbidden-root",
                str(forbidden),
                "export",
                "--output",
                str(alias),
            ),
            succeeds=False,
        )

        self.assertIn("symlink", failed["error"])
        self.assertFalse(escaped.exists())
        status = self.cli("status")
        self.assertEqual(status["verification"]["failures"], [])
        self.assertEqual(status["foreign_key_failures"], 0)
        self.assertTrue(self.catalogue.is_file())
        self.assertFalse((self.root / "Inventory.md").exists())

    def test_canonical_store_symlinks_fail_closed(self) -> None:
        self.initialize()
        data = self.root / "Data"
        store = data / "store"
        forbidden = self.scratch / "forbidden"
        forbidden.mkdir()

        outside_data = forbidden / "Data"
        shutil.move(data, outside_data)
        data.symlink_to(outside_data, target_is_directory=True)
        for read_command in (("search", "anything"), ("show", "itm-anything")):
            with self.subTest(read_command=read_command[0]):
                data_failed = self.execute(
                    self.command(*read_command),
                    succeeds=False,
                )
                self.assertIn("symlink", data_failed["error"])
        data.unlink()
        shutil.move(outside_data, data)
        store = data / "store"

        outside_store = forbidden / "store"
        shutil.move(store, outside_store)
        store.symlink_to(outside_store, target_is_directory=True)

        failed = self.execute(
            self.command("--forbidden-root", str(forbidden), "status"),
            succeeds=False,
        )
        self.assertIn("symlink", failed["error"])
        self.assertTrue((outside_store / "items.jsonl").is_file())

        store.unlink()
        shutil.move(outside_store, store)
        outside_table = forbidden / "items.jsonl"
        shutil.move(store / "items.jsonl", outside_table)
        (store / "items.jsonl").symlink_to(outside_table)
        table_failed = self.execute(self.command("status"), succeeds=False)
        self.assertIn("symlink", table_failed["error"])
        self.assertTrue(outside_table.is_file())

        standalone_database = self.scratch / "standalone.sqlite"
        rebuilt = subprocess.run(
            [
                sys.executable,
                str(HERE / "rebuild_inventory_sqlite.py"),
                "--store",
                str(store),
                "--database",
                str(standalone_database),
            ],
            text=True,
            capture_output=True,
            check=False,
        )
        self.assertNotEqual(rebuilt.returncode, 0)
        self.assertIn("symlink", rebuilt.stderr)
        self.assertFalse(standalone_database.exists())

        verified = subprocess.run(
            [
                sys.executable,
                str(HERE / "verify_inventory.py"),
                "--store",
                str(store),
                "--database",
                str(self.runtime / "inventory.sqlite"),
                "--markdown",
                str(self.catalogue),
            ],
            text=True,
            capture_output=True,
            check=False,
        )
        self.assertNotEqual(verified.returncode, 0)
        self.assertIn("symlink", verified.stdout)

        (store / "items.jsonl").unlink()
        shutil.move(outside_table, store / "items.jsonl")
        standalone_store_link = self.scratch / "standalone-store-link"
        standalone_store_link.symlink_to(store, target_is_directory=True)
        linked_store_database = self.scratch / "linked-store.sqlite"
        linked_store_rebuild = subprocess.run(
            [
                sys.executable,
                str(HERE / "rebuild_inventory_sqlite.py"),
                "--store",
                str(standalone_store_link),
                "--database",
                str(linked_store_database),
            ],
            text=True,
            capture_output=True,
            check=False,
        )
        self.assertNotEqual(linked_store_rebuild.returncode, 0)
        self.assertIn("symlink", linked_store_rebuild.stderr)
        self.assertFalse(linked_store_database.exists())

    def test_runtime_children_and_database_symlinks_fail_closed(self) -> None:
        self.initialize()

        external_backups = self.scratch / "external-backups"
        external_backups.mkdir()
        backups = self.runtime / "backups"
        backups.rmdir()
        backups.symlink_to(external_backups, target_is_directory=True)
        backup_failed = self.execute(
            self.command(
                "add-location",
                "--name",
                "Must not escape",
                "--location-id",
                "loc-must-not-escape",
                "--kind",
                "room",
            ),
            succeeds=False,
        )
        self.assertIn("symlink", backup_failed["error"])
        self.assertEqual(list(external_backups.iterdir()), [])
        backups.unlink()
        backups.mkdir()

        external_proposals = self.scratch / "external-proposals"
        external_proposals.mkdir()
        proposals = self.runtime / "proposals"
        proposals.symlink_to(external_proposals, target_is_directory=True)
        operations = self.scratch / "proposal.json"
        operations.write_text(
            json.dumps(
                [
                    [
                        "add-location",
                        "--name",
                        "Must remain local",
                        "--location-id",
                        "loc-must-remain-local",
                        "--kind",
                        "room",
                    ]
                ]
            )
        )
        proposal_failed = self.execute(
            self.command("propose", "--operations", str(operations)),
            succeeds=False,
        )
        self.assertIn("symlink", proposal_failed["error"])
        self.assertEqual(list(external_proposals.iterdir()), [])
        proposals.unlink()

        database = self.runtime / "inventory.sqlite"
        database.unlink()
        external_database = self.scratch / "external-inventory.sqlite"
        sentinel = b"database sentinel must survive\n"
        external_database.write_bytes(sentinel)
        database.symlink_to(external_database)
        database_failed = self.execute(self.command("status"), succeeds=False)
        self.assertIn("managed private tree", database_failed["error"])
        self.assertIn("symlink", database_failed["error"])
        self.assertEqual(external_database.read_bytes(), sentinel)

    def test_orphan_transaction_workspace_is_preserved_for_inspection(self) -> None:
        self.initialize()
        workspace = self.runtime / ".property-inventory-transaction"
        workspace.mkdir()
        sentinel = workspace / "unexpected-evidence"
        sentinel.write_bytes(b"preserve me")

        failed = self.execute(self.command("status"), succeeds=False)

        self.assertIn("without a journal", failed["error"])
        self.assertEqual(sentinel.read_bytes(), b"preserve me")

    def test_file_digest_reads_large_files_in_bounded_chunks(self) -> None:
        class RecordingReader(io.BytesIO):
            def __init__(self, payload: bytes) -> None:
                super().__init__(payload)
                self.requested_sizes: list[int] = []

            def read(self, size: int = -1) -> bytes:
                self.requested_sizes.append(size)
                return super().read(size)

        class RecordingPath:
            def __init__(self, payload: bytes) -> None:
                self.reader = RecordingReader(payload)

            def open(self, mode: str) -> RecordingReader:
                self.assert_mode = mode
                return self.reader

        payload = b"x" * (2 * 1024 * 1024 + 17)
        path = RecordingPath(payload)

        digest = file_digest(path)  # type: ignore[arg-type]

        self.assertEqual(digest, self.digest(payload))
        self.assertEqual(path.assert_mode, "rb")
        self.assertGreaterEqual(len(path.reader.requested_sizes), 4)
        self.assertEqual(set(path.reader.requested_sizes), {1024 * 1024})

    def test_invalid_utf8_config_returns_structured_json_error(self) -> None:
        invalid_config = self.scratch / "invalid-config.json"
        invalid_config.write_bytes(b"\xff\xfe\x00")

        failed = self.execute(
            [sys.executable, str(CLI), "--config", str(invalid_config), "status"],
            succeeds=False,
        )

        self.assertIn("cannot read config file", failed["error"])

    def test_in_process_execution_restores_instance_environment(self) -> None:
        keys = (
            "PROPERTY_INVENTORY_MEDIA_ROOT",
            "PROPERTY_INVENTORY_CATALOGUE_OUTPUT",
            "PROPERTY_INVENTORY_CATALOGUE_SCOPE",
        )
        original = {key: os.environ.get(key) for key in keys}
        inherited_media = self.scratch / "inherited-media"
        inherited_catalogue = self.scratch / "inherited" / "Inventory.md"
        try:
            os.environ["PROPERTY_INVENTORY_MEDIA_ROOT"] = str(inherited_media)
            os.environ["PROPERTY_INVENTORY_CATALOGUE_OUTPUT"] = str(inherited_catalogue)
            os.environ["PROPERTY_INVENTORY_CATALOGUE_SCOPE"] = "public"
            first_root = self.scratch / "in-process-first"
            first_runtime = self.scratch / "in-process-first-runtime"
            first_media = self.scratch / "in-process-first-media"
            first_catalogue = self.scratch / "in-process-first-catalogue" / "Inventory.md"
            second_root = self.scratch / "in-process-second"
            second_runtime = self.scratch / "in-process-second-runtime"
            second_media = self.scratch / "in-process-second-media"
            second_catalogue = self.scratch / "in-process-second-catalogue" / "Inventory.md"

            execute_in_process(
                self.command_for(
                    first_root,
                    first_runtime,
                    first_media,
                    first_catalogue,
                    "--catalogue-scope",
                    "personal",
                    "init",
                )[2:]
            )
            self.assertEqual(os.environ["PROPERTY_INVENTORY_MEDIA_ROOT"], str(inherited_media))
            self.assertEqual(
                os.environ["PROPERTY_INVENTORY_CATALOGUE_OUTPUT"], str(inherited_catalogue)
            )
            self.assertEqual(os.environ["PROPERTY_INVENTORY_CATALOGUE_SCOPE"], "public")
            execute_in_process(
                self.command_for(
                    second_root,
                    second_runtime,
                    second_media,
                    second_catalogue,
                    "--catalogue-scope",
                    "public",
                    "init",
                )[2:]
            )
            self.assertTrue(first_catalogue.is_file())
            self.assertTrue(second_catalogue.is_file())
            self.assertFalse(inherited_catalogue.exists())
        finally:
            for key, value in original.items():
                if value is None:
                    os.environ.pop(key, None)
                else:
                    os.environ[key] = value

    def test_concurrent_in_process_calls_keep_instance_paths_isolated(self) -> None:
        barrier = threading.Barrier(2)
        observed: dict[str, tuple[Path | None, Path, str]] = {}
        failures: list[BaseException] = []

        def inspect_paths(args: object) -> dict:
            barrier.wait()
            paths = data_paths(args.inventory_root, args.runtime_dir)
            observed[args.inventory_root.name] = (
                paths["media_root"],
                paths["catalogue"],
                paths["catalogue_scope"],
            )
            barrier.wait()
            return {"status": "isolated"}

        def run(name: str, scope: str) -> None:
            try:
                execute_in_process(
                    self.command_for(
                        self.scratch / name,
                        self.scratch / f"{name}-runtime",
                        self.scratch / f"{name}-media",
                        self.scratch / f"{name}-catalogue" / "Inventory.md",
                        "--catalogue-scope",
                        scope,
                        "status",
                    )[2:]
                )
            except BaseException as error:
                failures.append(error)

        with patch("property_inventory.cli.command_status", side_effect=inspect_paths):
            threads = [
                threading.Thread(target=run, args=("thread-a", "personal")),
                threading.Thread(target=run, args=("thread-b", "public")),
            ]
            for thread in threads:
                thread.start()
            for thread in threads:
                thread.join(timeout=10)

        self.assertFalse(failures)
        self.assertEqual(
            observed,
            {
                "thread-a": (
                    (self.scratch / "thread-a-media").resolve(),
                    (self.scratch / "thread-a-catalogue" / "Inventory.md").resolve(),
                    "personal",
                ),
                "thread-b": (
                    (self.scratch / "thread-b-media").resolve(),
                    (self.scratch / "thread-b-catalogue" / "Inventory.md").resolve(),
                    "public",
                ),
            },
        )

    def test_malformed_auxiliary_name_returns_json_error(self) -> None:
        self.initialize()

        failed = self.execute(
            self.command("auxiliary-manifest", "--include", "."),
            succeeds=False,
        )

        self.assertIn("unsafe auxiliary-data path", failed["error"])
    def test_export_and_blank_restore_preserve_store_auxiliaries_and_media(self) -> None:
        self.initialize()
        source_auxiliaries = self.write_auxiliary_fixture()
        self.assertEqual(self.cli("status")["verification"]["failures"], [])

        evidence_id = self.cli(
            "order",
            "--actor",
            "Instance export fixture",
            "--source-ref",
            "Instance export order fixture",
            "--name",
            "instance-export-object",
            "--category",
            "test fixture",
            "--ordered-on",
            "2026-08-05",
            "--order-placed",
        )["result"]["evidence_id"]
        media_payload = b"instance export media bytes\x00\xff"
        media_source = self.scratch / "evidence.bin"
        media_source.write_bytes(media_payload)
        attached = self.cli(
            "attach-media",
            "--evidence-id",
            evidence_id,
            "--file",
            str(media_source),
            "--role",
            "source",
        )
        media_digest = attached["result"]["sha256"]

        source_store = {
            path.name: path.read_bytes()
            for path in sorted((self.root / "Data" / "store").glob("*.jsonl"))
        }
        archive_path = self.scratch / "instance.tar.gz"
        exported = self.cli("export", "--output", str(archive_path))
        self.assertEqual(exported["status"], "exported")

        with tarfile.open(archive_path, "r:gz") as archive:
            members = {member.name for member in archive.getmembers()}
            export_manifest_file = archive.extractfile("manifest.json")
            self.assertIsNotNone(export_manifest_file)
            export_manifest = json.loads(export_manifest_file.read())
        expected_store_members = {f"store/{name}" for name in source_store}
        expected_auxiliary_members = {f"auxiliary/{name}" for name in source_auxiliaries}
        self.assertTrue(expected_store_members.issubset(members))
        self.assertTrue(expected_auxiliary_members.issubset(members))
        self.assertIn(f"media/{media_digest}", members)
        self.assertEqual(
            export_manifest["auxiliary"],
            {name: self.digest(payload) for name, payload in source_auxiliaries.items()},
        )

        restored_root = self.scratch / "restored-inventory"
        restored_runtime = self.scratch / "restored-runtime"
        restored_media = self.scratch / "restored-media"
        restored_catalogue = self.scratch / "restored-catalogue" / "Inventory.md"
        restored = self.execute(
            self.command_for(
                restored_root,
                restored_runtime,
                restored_media,
                restored_catalogue,
                "restore",
                "--archive",
                str(archive_path),
            )
        )
        self.assertEqual(restored["status"], "restored")
        restored_store = {
            path.name: path.read_bytes()
            for path in sorted((restored_root / "Data" / "store").glob("*.jsonl"))
        }
        restored_auxiliaries = {
            name: (restored_root / "Data" / name).read_bytes() for name in source_auxiliaries
        }
        self.assertEqual(restored_store, source_store)
        self.assertEqual(restored_auxiliaries, source_auxiliaries)
        self.assertEqual(
            (restored_media / "sha256" / media_digest[:2] / media_digest).read_bytes(),
            media_payload,
        )
        self.assertEqual(
            (restored_root / ".gitignore").read_text(),
            "/.property-inventory-runtime.json\n",
        )
        restored_binding = json.loads(
            (restored_root / ".property-inventory-runtime.json").read_text()
        )
        self.assertEqual(restored_binding["format"], 2)
        self.assertEqual(
            restored_binding["runtime_dir"], str(restored_runtime.resolve())
        )
        self.assertRegex(
            restored_binding["installation_id"],
            r"^[0-9a-f]{8}(?:-[0-9a-f]{4}){3}-[0-9a-f]{12}$",
        )
        self.assertTrue(restored_catalogue.is_file())
        self.assertFalse((restored_root / "Inventory.md").exists())
        restored_status = self.execute(
            self.command_for(
                restored_root,
                restored_runtime,
                restored_media,
                restored_catalogue,
                "status",
            )
        )
        self.assertEqual(restored_status["verification"]["failures"], [])
        self.assertEqual(restored_status["foreign_key_failures"], 0)

    def test_status_fails_for_missing_or_tampered_required_auxiliary(self) -> None:
        self.initialize()
        original = self.write_auxiliary_fixture()
        self.assertEqual(self.cli("status")["verification"]["failures"], [])

        source_inventory = self.root / "Data" / "source-inventory.json"
        source_inventory.unlink()
        missing = self.execute(self.command("status"), succeeds=False)
        missing_message = json.dumps(missing, sort_keys=True).lower()
        self.assertIn("source-inventory.json", missing_message)
        self.assertIn("missing", missing_message)

        source_inventory.write_bytes(original["source-inventory.json"])
        self.assertEqual(self.cli("status")["verification"]["failures"], [])
        policy = self.root / "Data" / "verification_policy.json"
        policy.write_bytes(b"{}\n")
        tampered = self.execute(self.command("status"), succeeds=False)
        tampered_message = json.dumps(tampered, sort_keys=True).lower()
        self.assertIn("verification_policy.json", tampered_message)
        self.assertTrue(
            any(term in tampered_message for term in ("sha256", "hash", "digest")),
            tampered_message,
        )

    def test_nested_undeclared_auxiliary_fails_closed(self) -> None:
        self.initialize()
        self.write_auxiliary_fixture()
        undeclared = self.root / "Data" / "new" / "nested" / "machine-input.bin"
        undeclared.parent.mkdir(parents=True)
        undeclared.write_bytes(b"must not be ignored")

        failed = self.execute(self.command("status"), succeeds=False)

        self.assertIn("new/nested/machine-input.bin", failed["error"])
        self.assertIn("not declared", failed["error"])

    def test_auxiliary_symlink_component_fails_before_export(self) -> None:
        self.initialize()
        self.write_auxiliary_fixture()
        source_directory = self.root / "Data" / "source"
        shutil.rmtree(source_directory)
        outside = self.scratch / "outside"
        outside.mkdir()
        (outside / "raw").mkdir()
        (outside / "raw" / "cold-source.bin").write_bytes(b"outside bytes")
        source_directory.symlink_to(outside, target_is_directory=True)

        failed = self.execute(
            self.command("export", "--output", str(self.scratch / "unsafe.tar.gz")),
            succeeds=False,
        )

        self.assertIn("symlink", failed["error"])
        self.assertFalse((self.scratch / "unsafe.tar.gz").exists())

    def test_manifest_replace_retains_existing_declarations_and_rehashes(self) -> None:
        self.initialize()
        original = self.write_auxiliary_fixture()
        cold_source = self.root / "Data" / "source" / "raw" / "cold-source.bin"
        cold_source.write_bytes(b"replacement source bytes")

        replaced = self.cli("auxiliary-manifest", "--replace")
        manifest = json.loads((self.root / "Data" / "auxiliary-manifest.json").read_text())

        self.assertIn("source/raw/cold-source.bin", replaced["declared_files"])
        self.assertEqual(
            manifest["files"]["source/raw/cold-source.bin"]["sha256"],
            self.digest(b"replacement source bytes"),
        )
        self.assertNotEqual(
            manifest["files"]["source/raw/cold-source.bin"]["sha256"],
            self.digest(original["source/raw/cold-source.bin"]),
        )
        self.assertEqual(self.cli("status")["verification"]["failures"], [])

    def test_manifest_remove_is_explicit_and_refuses_existing_bytes(self) -> None:
        self.initialize()
        self.write_auxiliary_fixture()
        relative = "source/raw/cold-source.bin"
        source = self.root / "Data" / relative

        rejected = self.execute(
            self.command(
                "auxiliary-manifest",
                "--replace",
                "--remove",
                relative,
            ),
            succeeds=False,
        )
        self.assertIn("not declared", rejected["error"])
        manifest = json.loads((self.root / "Data" / "auxiliary-manifest.json").read_text())
        self.assertIn(relative, manifest["files"])

        source.unlink()
        removed = self.cli(
            "auxiliary-manifest",
            "--replace",
            "--remove",
            relative,
        )
        self.assertNotIn(relative, removed["declared_files"])
        manifest = json.loads((self.root / "Data" / "auxiliary-manifest.json").read_text())
        self.assertNotIn(relative, manifest["files"])
        self.assertEqual(self.cli("status")["verification"]["failures"], [])

    def test_legacy_empty_fixture_without_auxiliaries_remains_portable(self) -> None:
        self.initialize()
        data = self.root / "Data"
        self.assertFalse((data / "auxiliary-manifest.json").exists())
        self.assertFalse((data / "verification_policy.json").exists())
        self.assertFalse((data / "source-inventory.json").exists())
        self.assertFalse((data / "account-candidates.json").exists())
        status = self.cli("status")
        self.assertEqual(status["verification"]["failures"], [])

        archive_path = self.scratch / "legacy-empty.tar.gz"
        self.cli("export", "--output", str(archive_path))
        restored_root = self.scratch / "legacy-restored"
        restored_runtime = self.scratch / "legacy-runtime"
        restored_media = self.scratch / "legacy-media"
        restored_catalogue = self.scratch / "legacy-catalogue" / "Inventory.md"
        restored = self.execute(
            self.command_for(
                restored_root,
                restored_runtime,
                restored_media,
                restored_catalogue,
                "restore",
                "--archive",
                str(archive_path),
            )
        )
        self.assertEqual(restored["status"], "restored")
        self.assertFalse((restored_root / "Data" / "auxiliary-manifest.json").exists())
        restored_status = self.execute(
            self.command_for(
                restored_root,
                restored_runtime,
                restored_media,
                restored_catalogue,
                "status",
            )
        )
        self.assertEqual(restored_status["verification"]["failures"], [])
        self.assertEqual(restored_status["foreign_key_failures"], 0)

    def test_format_1_restore_requires_explicit_degraded_opt_in(self) -> None:
        self.initialize()
        current_export = self.scratch / "current.tar.gz"
        self.cli("export", "--output", str(current_export))
        legacy_export = self.scratch / "legacy-format-1.tar.gz"
        with tarfile.open(current_export, "r:gz") as source, tarfile.open(
            legacy_export, "w:gz"
        ) as target:
            manifest_file = source.extractfile("manifest.json")
            self.assertIsNotNone(manifest_file)
            manifest = json.loads(manifest_file.read())
            manifest["format"] = 1
            manifest.pop("auxiliary")
            manifest_payload = (json.dumps(manifest, sort_keys=True) + "\n").encode()
            manifest_info = tarfile.TarInfo("manifest.json")
            manifest_info.size = len(manifest_payload)
            target.addfile(manifest_info, io.BytesIO(manifest_payload))
            for member in source.getmembers():
                if member.name.startswith("store/"):
                    payload = source.extractfile(member)
                    self.assertIsNotNone(payload)
                    target.addfile(member, payload)

        rejected_root = self.scratch / "legacy-rejected"
        rejected = self.execute(
            self.command_for(
                rejected_root,
                self.scratch / "legacy-rejected-runtime",
                self.scratch / "legacy-rejected-media",
                self.scratch / "legacy-rejected-catalogue" / "Inventory.md",
                "restore",
                "--archive",
                str(legacy_export),
            ),
            succeeds=False,
        )
        self.assertIn("allow-unsafe-legacy", rejected["error"])
        self.assertFalse(rejected_root.exists())

        restored = self.execute(
            self.command_for(
                self.scratch / "legacy-restored-opt-in",
                self.scratch / "legacy-restored-opt-in-runtime",
                self.scratch / "legacy-restored-opt-in-media",
                self.scratch / "legacy-restored-opt-in-catalogue" / "Inventory.md",
                "restore",
                "--archive",
                str(legacy_export),
                "--allow-unsafe-legacy",
            )
        )
        self.assertEqual(restored["status"], "restored_unsafe_legacy")
        self.assertEqual(restored["checks"]["verification"]["status"], "degraded_unsafe_legacy")
        self.assertEqual(
            restored["degraded_reasons"],
            ["legacy format-1 export had no auxiliary-data manifest"],
        )
        degraded_root = self.scratch / "legacy-restored-opt-in"
        degraded_runtime = self.scratch / "legacy-restored-opt-in-runtime"
        degraded_media = self.scratch / "legacy-restored-opt-in-media"
        degraded_catalogue = (
            self.scratch / "legacy-restored-opt-in-catalogue" / "Inventory.md"
        )
        later_status = self.execute(
            self.command_for(
                degraded_root,
                degraded_runtime,
                degraded_media,
                degraded_catalogue,
                "status",
            )
        )
        self.assertEqual(later_status["status"], "degraded_unsafe_legacy")
        blocked_export = self.execute(
            self.command_for(
                degraded_root,
                degraded_runtime,
                degraded_media,
                degraded_catalogue,
                "export",
                "--output",
                str(self.scratch / "must-not-be-trusted.tar.gz"),
            ),
            succeeds=False,
        )
        self.assertIn("degraded inventory", blocked_export["error"])

    def test_restore_rejects_malformed_manifest_as_structured_error(self) -> None:
        malformed = self.scratch / "malformed-manifest.tar.gz"
        payload = b"[]\n"
        with tarfile.open(malformed, "w:gz") as archive:
            info = tarfile.TarInfo("manifest.json")
            info.size = len(payload)
            archive.addfile(info, io.BytesIO(payload))

        failed = self.execute(
            self.command_for(
                self.scratch / "malformed-inventory",
                self.scratch / "malformed-runtime",
                self.scratch / "malformed-media",
                self.scratch / "malformed-catalogue" / "Inventory.md",
                "restore",
                "--archive",
                str(malformed),
            ),
            succeeds=False,
        )

        self.assertIn("manifest must be an object", failed["error"])

    def test_restore_rejects_archive_catalogue_alias_without_overwrite(self) -> None:
        self.initialize()
        archive_path = self.scratch / "sole-backup.tar.gz"
        self.cli("export", "--output", str(archive_path))
        original = archive_path.read_bytes()

        failed = self.execute(
            self.command_for(
                self.scratch / "alias-inventory",
                self.scratch / "alias-runtime",
                self.scratch / "alias-media",
                archive_path,
                "restore",
                "--archive",
                str(archive_path),
            ),
            succeeds=False,
        )

        self.assertIn("different files", failed["error"])
        self.assertEqual(archive_path.read_bytes(), original)

    def test_export_rejects_catalogue_alias_and_managed_namespaces(self) -> None:
        self.initialize()
        original_catalogue = self.catalogue.read_bytes()

        alias = self.execute(
            self.command("export", "--output", str(self.catalogue)),
            succeeds=False,
        )
        self.assertIn("different files", alias["error"])
        self.assertEqual(self.catalogue.read_bytes(), original_catalogue)

        for label, output in (
            ("inventory", self.root / "backup.tar.gz"),
            ("runtime", self.runtime / "backup.tar.gz"),
            ("media", self.media / "backup.tar.gz"),
        ):
            with self.subTest(label=label):
                rejected = self.execute(
                    self.command("export", "--output", str(output)),
                    succeeds=False,
                )
                self.assertIn(f"outside the {label} namespace", rejected["error"])
                self.assertFalse(output.exists())

    def test_restore_rejects_media_not_declared_by_canonical_rows(self) -> None:
        self.initialize()
        source_archive = self.scratch / "canonical-export.tar.gz"
        self.cli("export", "--output", str(source_archive))
        foreign_payload = b"content-valid but unreferenced media"
        foreign_digest = self.digest(foreign_payload)
        foreign_archive = self.scratch / "foreign-media-export.tar.gz"

        with tarfile.open(source_archive, "r:gz") as source, tarfile.open(
            foreign_archive, "w:gz"
        ) as target:
            manifest_file = source.extractfile("manifest.json")
            self.assertIsNotNone(manifest_file)
            manifest = json.loads(manifest_file.read())
            manifest["media"][foreign_digest] = {
                "byte_size": len(foreign_payload),
                "path": f"media/{foreign_digest}",
            }
            manifest_payload = (json.dumps(manifest, sort_keys=True) + "\n").encode()
            info = tarfile.TarInfo("manifest.json")
            info.size = len(manifest_payload)
            target.addfile(info, io.BytesIO(manifest_payload))
            for member in source.getmembers():
                if member.name == "manifest.json":
                    continue
                member_file = source.extractfile(member)
                self.assertIsNotNone(member_file)
                target.addfile(member, member_file)
            media_info = tarfile.TarInfo(f"media/{foreign_digest}")
            media_info.size = len(foreign_payload)
            target.addfile(media_info, io.BytesIO(foreign_payload))

        failed = self.execute(
            self.command_for(
                self.scratch / "foreign-inventory",
                self.scratch / "foreign-runtime",
                self.scratch / "foreign-media",
                self.scratch / "foreign-catalogue" / "Inventory.md",
                "restore",
                "--archive",
                str(foreign_archive),
            ),
            succeeds=False,
        )

        self.assertIn("canonical media_assets", failed["error"])

    def test_invalid_restore_journal_cannot_delete_external_paths(self) -> None:
        self.initialize()
        archive_path = self.scratch / "journal-source.tar.gz"
        self.cli("export", "--output", str(archive_path))
        runtime = self.scratch / "invalid-journal-runtime"
        runtime.mkdir()
        external_workspace = self.scratch / "external-workspace"
        external_workspace.mkdir()
        (external_workspace / "sentinel").write_bytes(b"workspace sentinel")
        external_catalogue = self.scratch / "external-catalogue.md"
        external_catalogue.write_bytes(b"catalogue sentinel")
        journal_path = runtime / ".property-inventory-restore.json"
        journal_path.write_text(
            json.dumps(
                {
                    "format": 1,
                    "inventory_root": str(self.scratch / "wrong-inventory"),
                    "media_root": str(self.scratch / "invalid-journal-media"),
                    "catalogue": str(external_catalogue),
                    "runtime": str(runtime),
                    "workspace": str(external_workspace),
                }
            )
        )

        failed = self.execute(
            self.command_for(
                self.scratch / "invalid-journal-inventory",
                runtime,
                self.scratch / "invalid-journal-media",
                external_catalogue,
                "restore",
                "--archive",
                str(archive_path),
            ),
            succeeds=False,
        )

        self.assertIn("restore journal is malformed", failed["error"])
        self.assertEqual(
            (external_workspace / "sentinel").read_bytes(), b"workspace sentinel"
        )
        self.assertEqual(external_catalogue.read_bytes(), b"catalogue sentinel")
        self.assertTrue(journal_path.exists())

    def test_restore_recovery_preserves_unexpected_post_crash_bytes(self) -> None:
        self.initialize()
        archive_path = self.scratch / "conflict-source.tar.gz"
        self.cli("export", "--output", str(archive_path))

        media_root = self.scratch / "conflict-media"
        media_runtime = self.scratch / "conflict-media-runtime"
        media_inventory = self.scratch / "conflict-media-inventory"
        media_catalogue = self.scratch / "conflict-media-catalogue" / "Inventory.md"
        environment = dict(os.environ)
        environment["PROPERTY_INVENTORY_FAIL_AFTER_RESTORE_REPLACEMENT"] = "media"
        crashed = subprocess.run(
            self.command_for(
                media_inventory,
                media_runtime,
                media_root,
                media_catalogue,
                "restore",
                "--archive",
                str(archive_path),
            ),
            text=True,
            capture_output=True,
            check=False,
            env=environment,
        )
        self.assertEqual(crashed.returncode, 94)
        foreign_media = media_root / "foreign-after-crash"
        foreign_media.write_bytes(b"do not delete")

        media_failed = self.execute(
            self.command_for(
                media_inventory,
                media_runtime,
                media_root,
                media_catalogue,
                "restore",
                "--archive",
                str(archive_path),
            ),
            succeeds=False,
        )
        self.assertIn("proven target", media_failed["error"])
        self.assertEqual(foreign_media.read_bytes(), b"do not delete")
        self.assertTrue((media_runtime / ".property-inventory-restore.json").exists())

        catalogue_root = self.scratch / "conflict-catalogue-inventory"
        catalogue_runtime = self.scratch / "conflict-catalogue-runtime"
        catalogue_media = self.scratch / "conflict-catalogue-media"
        catalogue = self.scratch / "conflict-catalogue" / "Inventory.md"
        catalogue.parent.mkdir()
        catalogue.write_bytes(b"original catalogue")
        environment["PROPERTY_INVENTORY_FAIL_AFTER_RESTORE_REPLACEMENT"] = "inventory"
        crashed = subprocess.run(
            self.command_for(
                catalogue_root,
                catalogue_runtime,
                catalogue_media,
                catalogue,
                "restore",
                "--archive",
                str(archive_path),
            ),
            text=True,
            capture_output=True,
            check=False,
            env=environment,
        )
        self.assertEqual(crashed.returncode, 95)
        catalogue.write_bytes(b"foreign catalogue after crash")

        catalogue_failed = self.execute(
            self.command_for(
                catalogue_root,
                catalogue_runtime,
                catalogue_media,
                catalogue,
                "restore",
                "--archive",
                str(archive_path),
            ),
            succeeds=False,
        )
        self.assertIn("changed outside", catalogue_failed["error"])
        self.assertEqual(catalogue.read_bytes(), b"foreign catalogue after crash")
        self.assertTrue(
            (catalogue_runtime / ".property-inventory-restore.json").exists()
        )

    def test_pending_restore_blocks_every_other_read_and_mutation(self) -> None:
        self.initialize()
        archive_path = self.scratch / "pending-restore-source.tar.gz"
        self.cli("export", "--output", str(archive_path))
        root = self.scratch / "pending-restore-inventory"
        runtime = self.scratch / "pending-restore-runtime"
        media = self.scratch / "pending-restore-media"
        catalogue = self.scratch / "pending-restore-catalogue" / "Inventory.md"
        environment = dict(os.environ)
        environment["PROPERTY_INVENTORY_FAIL_AFTER_RESTORE_REPLACEMENT"] = "inventory"
        crashed = subprocess.run(
            self.command_for(
                root,
                runtime,
                media,
                catalogue,
                "restore",
                "--archive",
                str(archive_path),
            ),
            text=True,
            capture_output=True,
            check=False,
            env=environment,
        )
        self.assertEqual(crashed.returncode, 95, crashed.stderr or crashed.stdout)

        for arguments in (
            ("status",),
            (
                "add-location",
                "--name",
                "Must not commit",
                "--location-id",
                "loc-must-not-commit",
                "--kind",
                "room",
            ),
        ):
            with self.subTest(command=arguments[0]):
                blocked = self.execute(
                    self.command_for(root, runtime, media, catalogue, *arguments),
                    succeeds=False,
                )
                self.assertIn("pending restore", blocked["error"])

        recovered = self.execute(
            self.command_for(
                root,
                runtime,
                media,
                catalogue,
                "restore",
                "--archive",
                str(archive_path),
            )
        )
        self.assertEqual(recovered["status"], "recovered_restored")
        self.assertEqual(recovered["checks"]["verification"]["failures"], [])

    def test_restore_refuses_unowned_runtime_bytes_and_untracked_workspace(self) -> None:
        self.initialize()
        archive_path = self.scratch / "runtime-boundary-source.tar.gz"
        self.cli("export", "--output", str(archive_path))
        root = self.scratch / "runtime-boundary-inventory"
        runtime = self.scratch / "runtime-boundary-runtime"
        media = self.scratch / "runtime-boundary-media"
        catalogue = self.scratch / "runtime-boundary-catalogue" / "Inventory.md"
        runtime.mkdir()
        sentinel = runtime / "inventory.sqlite"
        sentinel.write_bytes(b"unrelated database sentinel")

        database_failed = self.execute(
            self.command_for(
                root,
                runtime,
                media,
                catalogue,
                "restore",
                "--archive",
                str(archive_path),
            ),
            succeeds=False,
        )
        self.assertIn("non-empty", database_failed["error"])
        self.assertEqual(sentinel.read_bytes(), b"unrelated database sentinel")

        sentinel.unlink()
        orphan = runtime / ".property-inventory-restore-orphan"
        orphan.mkdir()
        private = orphan / "private.jsonl"
        private.write_bytes(b"private staged bytes")
        orphan_failed = self.execute(
            self.command_for(
                root,
                runtime,
                media,
                catalogue,
                "restore",
                "--archive",
                str(archive_path),
            ),
            succeeds=False,
        )
        self.assertIn("untracked restore workspace", orphan_failed["error"])
        self.assertEqual(private.read_bytes(), b"private staged bytes")

    def test_restore_recovery_rejects_tampered_staging_and_catalogue_preimage(self) -> None:
        self.initialize()
        archive_path = self.scratch / "staging-conflict-source.tar.gz"
        self.cli("export", "--output", str(archive_path))

        staged_root = self.scratch / "staged-conflict-inventory"
        staged_runtime = self.scratch / "staged-conflict-runtime"
        staged_media = self.scratch / "staged-conflict-media"
        staged_catalogue = self.scratch / "staged-conflict-catalogue" / "Inventory.md"
        environment = dict(os.environ)
        environment["PROPERTY_INVENTORY_FAIL_AFTER_RESTORE_REPLACEMENT"] = "media"
        crashed = subprocess.run(
            self.command_for(
                staged_root,
                staged_runtime,
                staged_media,
                staged_catalogue,
                "restore",
                "--archive",
                str(archive_path),
            ),
            text=True,
            capture_output=True,
            check=False,
            env=environment,
        )
        self.assertEqual(crashed.returncode, 94)
        workspace = next(staged_runtime.glob(".property-inventory-restore-*"))
        foreign_staged = workspace / "inventory" / "foreign-after-crash.txt"
        foreign_staged.write_bytes(b"preserve unexpected staged bytes")

        staged_failed = self.execute(
            self.command_for(
                staged_root,
                staged_runtime,
                staged_media,
                staged_catalogue,
                "restore",
                "--archive",
                str(archive_path),
            ),
            succeeds=False,
        )
        self.assertIn("staging changed", staged_failed["error"])
        self.assertEqual(foreign_staged.read_bytes(), b"preserve unexpected staged bytes")

        preimage_root = self.scratch / "preimage-conflict-inventory"
        preimage_runtime = self.scratch / "preimage-conflict-runtime"
        preimage_media = self.scratch / "preimage-conflict-media"
        preimage_catalogue = self.scratch / "preimage-conflict" / "Inventory.md"
        preimage_catalogue.parent.mkdir()
        original_catalogue = b"original external catalogue\n"
        preimage_catalogue.write_bytes(original_catalogue)
        environment["PROPERTY_INVENTORY_FAIL_AFTER_RESTORE_REPLACEMENT"] = "inventory"
        crashed = subprocess.run(
            self.command_for(
                preimage_root,
                preimage_runtime,
                preimage_media,
                preimage_catalogue,
                "restore",
                "--archive",
                str(archive_path),
            ),
            text=True,
            capture_output=True,
            check=False,
            env=environment,
        )
        self.assertEqual(crashed.returncode, 95)
        preimage_workspace = next(
            preimage_runtime.glob(".property-inventory-restore-*")
        )
        preimage = preimage_workspace / "catalogue.before"
        preimage.write_bytes(b"tampered staged preimage")

        preimage_failed = self.execute(
            self.command_for(
                preimage_root,
                preimage_runtime,
                preimage_media,
                preimage_catalogue,
                "restore",
                "--archive",
                str(archive_path),
            ),
            succeeds=False,
        )
        self.assertIn("corrupt catalogue preimage", preimage_failed["error"])
        self.assertEqual(preimage_catalogue.read_bytes(), original_catalogue)
        self.assertEqual(preimage.read_bytes(), b"tampered staged preimage")

    def test_restore_rolls_back_blank_roots_if_external_catalogue_commit_fails(self) -> None:
        self.initialize()
        archive_path = self.scratch / "rollback.tar.gz"
        self.cli("export", "--output", str(archive_path))
        restored_root = self.scratch / "rollback-inventory"
        restored_media = self.scratch / "rollback-media"
        blocked_parent = self.scratch / "blocked-catalogue-parent"
        blocked_parent.write_text("preserve")

        failed = self.execute(
            self.command_for(
                restored_root,
                self.scratch / "rollback-runtime",
                restored_media,
                blocked_parent / "Inventory.md",
                "restore",
                "--archive",
                str(archive_path),
            ),
            succeeds=False,
        )

        self.assertIn("blocked-catalogue-parent", failed["error"])
        self.assertFalse(restored_root.exists())
        self.assertFalse(restored_media.exists())
        self.assertEqual(blocked_parent.read_text(), "preserve")

    def test_restore_rollback_survives_process_death_for_absent_and_empty_roots(self) -> None:
        self.initialize()
        archive_path = self.scratch / "rollback-crash-source.tar.gz"
        self.cli("export", "--output", str(archive_path))
        for roots_existed in (False, True):
            for failure_label, expected_code in (("inventory", 89), ("media", 90)):
                with self.subTest(
                    roots_existed=roots_existed, failure_label=failure_label
                ):
                    suffix = f"{'empty' if roots_existed else 'absent'}-{failure_label}"
                    restored_root = self.scratch / f"rollback-crash-inventory-{suffix}"
                    restored_runtime = self.scratch / f"rollback-crash-runtime-{suffix}"
                    restored_media = self.scratch / f"rollback-crash-media-{suffix}"
                    restored_catalogue = (
                        self.scratch
                        / f"rollback-crash-catalogue-{suffix}"
                        / "Inventory.md"
                    )
                    if roots_existed:
                        restored_root.mkdir()
                        restored_media.mkdir()
                    environment = dict(os.environ)
                    environment[
                        "PROPERTY_INVENTORY_FAIL_RESTORE_AFTER_CATALOGUE_REPLACE"
                    ] = "1"
                    environment[
                        "PROPERTY_INVENTORY_FAIL_DURING_RESTORE_ROLLBACK"
                    ] = failure_label

                    crashed = subprocess.run(
                        self.command_for(
                            restored_root,
                            restored_runtime,
                            restored_media,
                            restored_catalogue,
                            "restore",
                            "--archive",
                            str(archive_path),
                        ),
                        text=True,
                        capture_output=True,
                        check=False,
                        env=environment,
                    )

                    self.assertEqual(
                        crashed.returncode,
                        expected_code,
                        crashed.stderr or crashed.stdout,
                    )
                    self.assertFalse(restored_catalogue.exists())
                    pending = json.loads(
                        (
                            restored_runtime / ".property-inventory-restore.json"
                        ).read_text()
                    )
                    self.assertEqual(pending["phase"], "rolling_back")

                    recovered = self.execute(
                        self.command_for(
                            restored_root,
                            restored_runtime,
                            restored_media,
                            restored_catalogue,
                            "restore",
                            "--archive",
                            str(archive_path),
                        )
                    )

                    self.assertEqual(recovered["status"], "restored")
                    self.assertEqual(
                        recovered["checks"]["verification"]["failures"], []
                    )
                    self.assertFalse(
                        (
                            restored_runtime / ".property-inventory-restore.json"
                        ).exists()
                    )
                    self.assertTrue(restored_root.is_dir())
                    self.assertTrue(restored_media.is_dir())

    def test_restore_rollback_cleanup_survives_process_death(self) -> None:
        self.initialize()
        archive_path = self.scratch / "rollback-cleanup-source.tar.gz"
        self.cli("export", "--output", str(archive_path))
        restored_root = self.scratch / "rollback-cleanup-inventory"
        restored_runtime = self.scratch / "rollback-cleanup-runtime"
        restored_media = self.scratch / "rollback-cleanup-media"
        restored_catalogue = self.scratch / "rollback-cleanup-catalogue" / "Inventory.md"
        environment = dict(os.environ)
        environment["PROPERTY_INVENTORY_FAIL_RESTORE_AFTER_CATALOGUE_REPLACE"] = "1"
        environment["PROPERTY_INVENTORY_FAIL_AFTER_RESTORE_ROLLBACK_STEP"] = (
            "cleanup-inventory"
        )

        crashed = subprocess.run(
            self.command_for(
                restored_root,
                restored_runtime,
                restored_media,
                restored_catalogue,
                "restore",
                "--archive",
                str(archive_path),
            ),
            text=True,
            capture_output=True,
            check=False,
            env=environment,
        )

        self.assertEqual(crashed.returncode, 94, crashed.stderr or crashed.stdout)
        pending = json.loads(
            (restored_runtime / ".property-inventory-restore.json").read_text()
        )
        self.assertEqual(pending["phase"], "rolled_back")

        recovered = self.execute(
            self.command_for(
                restored_root,
                restored_runtime,
                restored_media,
                restored_catalogue,
                "restore",
                "--archive",
                str(archive_path),
            )
        )

        self.assertEqual(recovered["status"], "restored")
        self.assertEqual(recovered["checks"]["verification"]["failures"], [])
        self.assertFalse(
            (restored_runtime / ".property-inventory-restore.json").exists()
        )

    def test_restore_rollback_cleanup_preserves_unexpected_workspace_bytes(self) -> None:
        self.initialize()
        archive_path = self.scratch / "rollback-workspace-source.tar.gz"
        self.cli("export", "--output", str(archive_path))
        restored_root = self.scratch / "rollback-workspace-inventory"
        restored_runtime = self.scratch / "rollback-workspace-runtime"
        restored_media = self.scratch / "rollback-workspace-media"
        restored_catalogue = self.scratch / "rollback-workspace-catalogue" / "Inventory.md"
        environment = dict(os.environ)
        environment["PROPERTY_INVENTORY_FAIL_RESTORE_AFTER_CATALOGUE_REPLACE"] = "1"
        environment["PROPERTY_INVENTORY_FAIL_AFTER_RESTORE_ROLLBACK_STEP"] = (
            "rolled-back"
        )

        crashed = subprocess.run(
            self.command_for(
                restored_root,
                restored_runtime,
                restored_media,
                restored_catalogue,
                "restore",
                "--archive",
                str(archive_path),
            ),
            text=True,
            capture_output=True,
            check=False,
            env=environment,
        )
        self.assertEqual(crashed.returncode, 93, crashed.stderr or crashed.stdout)
        journal_path = restored_runtime / ".property-inventory-restore.json"
        journal = json.loads(journal_path.read_text())
        workspace = Path(journal["workspace"])
        foreign = workspace / "foreign-after-crash"
        foreign.write_bytes(b"do not delete")

        failed = self.execute(
            self.command_for(
                restored_root,
                restored_runtime,
                restored_media,
                restored_catalogue,
                "restore",
                "--archive",
                str(archive_path),
            ),
            succeeds=False,
        )

        self.assertIn("unexpected entries", failed["error"])
        self.assertEqual(foreign.read_bytes(), b"do not delete")
        self.assertTrue(journal_path.exists())

    def test_restore_recovers_after_process_death_between_root_replacements(self) -> None:
        self.initialize()
        archive_path = self.scratch / "crash-recovery.tar.gz"
        self.cli("export", "--output", str(archive_path))
        for replacement, expected_code in (("media", 94), ("inventory", 95)):
            with self.subTest(replacement=replacement):
                restored_root = self.scratch / f"crash-{replacement}-inventory"
                restored_runtime = self.scratch / f"crash-{replacement}-runtime"
                restored_media = self.scratch / f"crash-{replacement}-media"
                restored_catalogue = (
                    self.scratch / f"crash-{replacement}-catalogue" / "Inventory.md"
                )
                environment = dict(os.environ)
                environment["PROPERTY_INVENTORY_FAIL_AFTER_RESTORE_REPLACEMENT"] = replacement
                crashed = subprocess.run(
                    self.command_for(
                        restored_root,
                        restored_runtime,
                        restored_media,
                        restored_catalogue,
                        "restore",
                        "--archive",
                        str(archive_path),
                    ),
                    text=True,
                    capture_output=True,
                    check=False,
                    env=environment,
                )
                self.assertEqual(crashed.returncode, expected_code, crashed.stderr or crashed.stdout)

                recovered = self.execute(
                    self.command_for(
                        restored_root,
                        restored_runtime,
                        restored_media,
                        restored_catalogue,
                        "restore",
                        "--archive",
                        str(archive_path),
                    )
                )
                self.assertEqual(recovered["status"], "recovered_restored")
                self.assertEqual(recovered["checks"]["verification"]["failures"], [])
                self.assertTrue(restored_catalogue.is_file())
                self.assertFalse(
                    (restored_runtime / ".property-inventory-restore.json").exists()
                )

    def test_restore_uses_target_sibling_transfer_and_recovers_before_install(self) -> None:
        self.initialize()
        archive_path = self.scratch / "transfer-recovery.tar.gz"
        self.cli("export", "--output", str(archive_path))
        for label, expected_code in (("media", 84), ("inventory", 85)):
            with self.subTest(label=label):
                restored_root = self.scratch / f"transfer-{label}-inventory"
                restored_runtime = self.scratch / f"transfer-{label}-runtime"
                restored_media = self.scratch / f"transfer-{label}-media"
                restored_catalogue = (
                    self.scratch / f"transfer-{label}-catalogue" / "Inventory.md"
                )
                environment = dict(os.environ)
                environment["PROPERTY_INVENTORY_FAIL_AFTER_RESTORE_TRANSFER"] = label

                crashed = subprocess.run(
                    self.command_for(
                        restored_root,
                        restored_runtime,
                        restored_media,
                        restored_catalogue,
                        "restore",
                        "--archive",
                        str(archive_path),
                    ),
                    text=True,
                    capture_output=True,
                    check=False,
                    env=environment,
                )

                self.assertEqual(
                    crashed.returncode, expected_code, crashed.stderr or crashed.stdout
                )
                journal = json.loads(
                    (restored_runtime / ".property-inventory-restore.json").read_text()
                )
                target = restored_media if label == "media" else restored_root
                transfer = restore_transfer_stage(target, journal["restore_id"], label)
                self.assertEqual(transfer.parent, target.parent)
                self.assertTrue(transfer.is_dir())

                recovered = self.execute(
                    self.command_for(
                        restored_root,
                        restored_runtime,
                        restored_media,
                        restored_catalogue,
                        "restore",
                        "--archive",
                        str(archive_path),
                    )
                )

                self.assertEqual(recovered["status"], "recovered_restored")
                self.assertFalse(transfer.exists())
                self.assertEqual(
                    recovered["checks"]["verification"]["failures"], []
                )

    def test_restore_recovers_an_interrupted_partial_transfer_copy(self) -> None:
        self.initialize()
        archive_path = self.scratch / "partial-transfer-source.tar.gz"
        self.cli("export", "--output", str(archive_path))
        root = self.scratch / "partial-transfer-inventory"
        runtime = self.scratch / "partial-transfer-runtime"
        media = self.scratch / "partial-transfer-media"
        catalogue = self.scratch / "partial-transfer-catalogue" / "Inventory.md"
        environment = dict(os.environ)
        environment["PROPERTY_INVENTORY_FAIL_AFTER_RESTORE_TRANSFER"] = "inventory"
        crashed = subprocess.run(
            self.command_for(
                root,
                runtime,
                media,
                catalogue,
                "restore",
                "--archive",
                str(archive_path),
            ),
            text=True,
            capture_output=True,
            check=False,
            env=environment,
        )
        self.assertEqual(crashed.returncode, 85, crashed.stderr or crashed.stdout)
        journal = json.loads(
            (runtime / ".property-inventory-restore.json").read_text()
        )
        transfer = restore_transfer_stage(root, journal["restore_id"], "inventory")
        build = restore_transfer_build(root, journal["restore_id"], "inventory")
        transfer.rename(build)
        copied_file = next(path for path in build.rglob("*") if path.is_file())
        staged_source = (
            Path(journal["workspace"])
            / "inventory"
            / copied_file.relative_to(build)
        )
        source_bytes = staged_source.read_bytes()
        copied_file.write_bytes(source_bytes[: max(1, len(source_bytes) // 2)])

        recovered = self.execute(
            self.command_for(
                root,
                runtime,
                media,
                catalogue,
                "restore",
                "--archive",
                str(archive_path),
            )
        )

        self.assertEqual(recovered["status"], "recovered_restored")
        self.assertFalse(build.exists())
        self.assertEqual(recovered["checks"]["verification"]["failures"], [])

    def test_restore_journal_write_failure_keeps_recovery_state_coherent(self) -> None:
        self.initialize()
        archive_path = self.scratch / "journal-write-source.tar.gz"
        self.cli("export", "--output", str(archive_path))
        root = self.scratch / "journal-write-inventory"
        runtime = self.scratch / "journal-write-runtime"
        media = self.scratch / "journal-write-media"
        catalogue = self.scratch / "journal-write-catalogue" / "Inventory.md"
        environment = dict(os.environ)
        environment["PROPERTY_INVENTORY_FAIL_AFTER_RESTORE_JOURNAL_WRITE"] = "1"

        failed = subprocess.run(
            self.command_for(
                root,
                runtime,
                media,
                catalogue,
                "restore",
                "--archive",
                str(archive_path),
            ),
            text=True,
            capture_output=True,
            check=False,
            env=environment,
        )
        self.assertNotEqual(failed.returncode, 0)
        self.assertIn("injected failure", failed.stderr)
        self.assertFalse(root.exists())
        self.assertFalse(media.exists())
        self.assertFalse((runtime / ".property-inventory-restore.json").exists())
        self.assertEqual(
            [path for path in runtime.glob(".property-inventory-restore-*")], []
        )

        restored = self.execute(
            self.command_for(
                root,
                runtime,
                media,
                catalogue,
                "restore",
                "--archive",
                str(archive_path),
            )
        )
        self.assertEqual(restored["checks"]["verification"]["failures"], [])

    def test_restore_recovers_after_committed_journal_before_cleanup(self) -> None:
        self.initialize()
        archive_path = self.scratch / "committed-crash-recovery.tar.gz"
        self.cli("export", "--output", str(archive_path))
        restored_root = self.scratch / "committed-crash-inventory"
        restored_runtime = self.scratch / "committed-crash-runtime"
        restored_media = self.scratch / "committed-crash-media"
        restored_catalogue = self.scratch / "committed-crash-catalogue" / "Inventory.md"
        environment = dict(os.environ)
        environment["PROPERTY_INVENTORY_FAIL_AFTER_RESTORE_COMMIT"] = "1"

        crashed = subprocess.run(
            self.command_for(
                restored_root,
                restored_runtime,
                restored_media,
                restored_catalogue,
                "restore",
                "--archive",
                str(archive_path),
            ),
            text=True,
            capture_output=True,
            check=False,
            env=environment,
        )
        self.assertEqual(crashed.returncode, 96, crashed.stderr or crashed.stdout)
        self.assertTrue((restored_runtime / ".property-inventory-restore.json").exists())

        recovered = self.execute(
            self.command_for(
                restored_root,
                restored_runtime,
                restored_media,
                restored_catalogue,
                "restore",
                "--archive",
                str(archive_path),
            )
        )

        self.assertEqual(recovered["status"], "recovered_restored")
        self.assertEqual(recovered["checks"]["verification"]["failures"], [])
        self.assertFalse((restored_runtime / ".property-inventory-restore.json").exists())

    def test_in_root_catalogue_recovers_after_committed_restore_crash(self) -> None:
        self.initialize()
        archive_path = self.scratch / "in-root-committed-recovery.tar.gz"
        self.cli("export", "--output", str(archive_path))
        restored_root = self.scratch / "in-root-inventory"
        restored_runtime = self.scratch / "in-root-runtime"
        restored_media = self.scratch / "in-root-media"
        command = [
            sys.executable,
            str(CLI),
            "--inventory-root",
            str(restored_root),
            "--runtime-dir",
            str(restored_runtime),
            "--media-root",
            str(restored_media),
            "restore",
            "--archive",
            str(archive_path),
        ]
        environment = dict(os.environ)
        environment["PROPERTY_INVENTORY_FAIL_AFTER_RESTORE_COMMIT"] = "1"
        crashed = subprocess.run(
            command,
            text=True,
            capture_output=True,
            check=False,
            env=environment,
        )
        self.assertEqual(crashed.returncode, 96, crashed.stderr or crashed.stdout)

        recovered = self.execute(command)

        self.assertEqual(recovered["status"], "recovered_restored")
        self.assertTrue((restored_root / "Inventory.md").is_file())
        self.assertFalse((restored_runtime / ".property-inventory-restore.json").exists())

    def test_restore_failure_after_catalogue_replacement_removes_new_catalogue(self) -> None:
        self.initialize()
        archive_path = self.scratch / "catalogue-rollback.tar.gz"
        self.cli("export", "--output", str(archive_path))
        restored_root = self.scratch / "catalogue-rollback-inventory"
        restored_runtime = self.scratch / "catalogue-rollback-runtime"
        restored_media = self.scratch / "catalogue-rollback-media"
        restored_catalogue = self.scratch / "catalogue-rollback" / "Inventory.md"
        environment = dict(os.environ)
        environment["PROPERTY_INVENTORY_FAIL_RESTORE_AFTER_CATALOGUE_REPLACE"] = "1"

        failed = subprocess.run(
            self.command_for(
                restored_root,
                restored_runtime,
                restored_media,
                restored_catalogue,
                "restore",
                "--archive",
                str(archive_path),
            ),
            text=True,
            capture_output=True,
            check=False,
            env=environment,
        )

        self.assertNotEqual(failed.returncode, 0, failed.stdout)
        self.assertIn("after external catalogue replacement", failed.stderr)
        self.assertFalse(restored_root.exists())
        self.assertFalse(restored_media.exists())
        self.assertFalse(restored_catalogue.exists())
        self.assertFalse((restored_runtime / ".property-inventory-restore.json").exists())


if __name__ == "__main__":
    unittest.main()
