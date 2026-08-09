#!/usr/bin/env python3
"""Adversarial installation ownership and restore-recovery acceptance tests."""

from __future__ import annotations

import hashlib
import json
import os
import shutil
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import patch

HERE = Path(__file__).resolve().parent
CLI = HERE / "property_inventory.py"
sys.path.insert(0, str(HERE / "src"))
from property_inventory import cli as cli_module  # noqa: E402

RUNTIME_BINDING = ".property-inventory-runtime.json"
RUNTIME_OWNER = ".property-inventory-owner.json"
RESTORE_JOURNAL = ".property-inventory-restore.json"


class InstallationRecoveryTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory(prefix="inventory-installation-recovery-")
        self.scratch = Path(self.temp.name)
        self.root = self.scratch / "source-inventory"
        self.runtime = self.scratch / "source-runtime"
        self.media = self.scratch / "source-media"
        self.catalogue = self.scratch / "source-catalogue" / "Inventory.md"
        self.assertEqual(self.cli("init")["status"], "initialized")

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
        return self.command_for(self.root, self.runtime, self.media, self.catalogue, *arguments)

    def execute(
        self, command: list[str], *, succeeds: bool = True, env: dict[str, str] | None = None
    ) -> dict:
        completed = subprocess.run(command, text=True, capture_output=True, check=False, env=env)
        if succeeds:
            self.assertEqual(completed.returncode, 0, completed.stderr or completed.stdout)
            return json.loads(completed.stdout)
        self.assertNotEqual(completed.returncode, 0, completed.stdout)
        return json.loads(completed.stderr)

    def cli(self, *arguments: str) -> dict:
        return self.execute(self.command(*arguments))

    @staticmethod
    def bytes_tree(root: Path) -> dict[str, bytes]:
        return {
            path.relative_to(root).as_posix(): path.read_bytes()
            for path in sorted(root.rglob("*"))
            if path.is_file()
        }

    @staticmethod
    def installation_id(root: Path) -> str:
        return json.loads((root / RUNTIME_BINDING).read_text())["installation_id"]

    def source_archive(self) -> Path:
        archive = self.scratch / "source.tar.gz"
        self.cli("export", "--output", str(archive))
        return archive

    def source_archive_with_media(self) -> Path:
        self.cli(
            "add-location", "--name", "Recovery fixture location", "--location-id", "loc-recovery",
            "--kind", "room",
        )
        item_id = self.cli(
            "order",
            "--actor",
            "Recovery test",
            "--source-ref",
            "recovery fixture order",
            "--name",
            "recovery fixture item",
            "--category",
            "test fixture",
            "--ordered-on",
            "2026-08-05",
            "--order-placed",
        )["result"]["item_id"]
        evidence_id = self.cli(
            "receive",
            "--actor",
            "Recovery test",
            "--source-ref",
            "recovery fixture receipt",
            "--item-id",
            item_id,
            "--received-on",
            "2026-08-06",
            "--location-id",
            "loc-recovery",
            "--physical-check",
        )["result"]["evidence_id"]
        source = self.scratch / "recovery-media.bin"
        source.write_bytes(b"recovery media bytes\x00\xff")
        self.cli(
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
        )
        return self.source_archive()

    def test_format_1_clone_never_auto_adopts_and_explicit_clone_init_is_distinct(self) -> None:
        original_store = self.bytes_tree(self.root / "Data" / "store")
        original_installation_id = self.installation_id(self.root)

        format_one_clone = self.scratch / "format-one-clone"
        shutil.copytree(self.root, format_one_clone)
        binding = format_one_clone / RUNTIME_BINDING
        binding.write_text(
            json.dumps({"format": 1, "runtime_dir": str(self.runtime.resolve())}),
            encoding="utf-8",
        )
        before_status = self.bytes_tree(format_one_clone)

        failed = self.execute(
            self.command_for(
                format_one_clone,
                self.scratch / "format-one-runtime",
                self.scratch / "format-one-media",
                self.scratch / "format-one-catalogue" / "Inventory.md",
                "status",
            ),
            succeeds=False,
        )
        self.assertIn("legacy inventory ownership is ambiguous", failed["error"])
        self.assertEqual(self.bytes_tree(format_one_clone), before_status)
        self.assertEqual(self.bytes_tree(self.root / "Data" / "store"), original_store)

        explicit_clone = self.scratch / "explicit-clone"
        shutil.copytree(self.root, explicit_clone)
        (explicit_clone / RUNTIME_BINDING).unlink()
        explicit_runtime = self.scratch / "explicit-runtime"
        explicit_media = self.scratch / "explicit-media"
        explicit_catalogue = self.scratch / "explicit-catalogue" / "Inventory.md"
        adopted = self.execute(
            self.command_for(
                explicit_clone,
                explicit_runtime,
                explicit_media,
                explicit_catalogue,
                "init",
            )
        )
        self.assertEqual(adopted["status"], "recovered_initialized")
        self.assertNotEqual(self.installation_id(explicit_clone), original_installation_id)
        self.assertEqual(self.bytes_tree(explicit_clone / "Data" / "store"), original_store)
        self.assertEqual(
            self.execute(
                self.command_for(
                    explicit_clone,
                    explicit_runtime,
                    explicit_media,
                    explicit_catalogue,
                    "status",
                )
            )["verification"]["failures"],
            [],
        )

    def test_explicit_clone_init_with_foreign_catalogue_leaves_no_adoption_state(self) -> None:
        clone = self.scratch / "foreign-catalogue-clone"
        shutil.copytree(self.root, clone)
        (clone / RUNTIME_BINDING).unlink()
        root_before = self.bytes_tree(clone)
        runtime = self.scratch / "foreign-catalogue-runtime"
        media = self.scratch / "foreign-catalogue-media"
        catalogue = self.scratch / "foreign-catalogue" / "Inventory.md"
        foreign_bytes = b"foreign catalogue bytes must survive\x00\xff"
        catalogue.parent.mkdir()
        catalogue.write_bytes(foreign_bytes)

        failed = self.execute(
            self.command_for(clone, runtime, media, catalogue, "init"), succeeds=False
        )

        self.assertIn("new catalogue output path", failed["error"])
        self.assertEqual(self.bytes_tree(clone), root_before)
        self.assertFalse((clone / RUNTIME_BINDING).exists())
        self.assertFalse(runtime.exists())
        self.assertEqual(catalogue.read_bytes(), foreign_bytes)

    def test_failed_bindingless_adoption_restores_empty_runtime_and_catalogue_parent(self) -> None:
        clone = self.scratch / "verifier-failure-clone"
        shutil.copytree(self.root, clone)
        (clone / RUNTIME_BINDING).unlink()
        root_before = self.bytes_tree(clone)
        runtime = self.scratch / "verifier-failure-runtime"
        runtime.mkdir()
        catalogue = self.scratch / "verifier-failure-catalogue" / "Inventory.md"
        catalogue.parent.mkdir()
        arguments = self.command_for(
            clone,
            runtime,
            self.scratch / "verifier-failure-media",
            catalogue,
            "init",
        )[2:]
        real_run = cli_module.run

        def fail_only_verification(command: list[str], *, cwd: Path | None = None) -> str:
            if Path(command[1]).name == "verify.py":
                return json.dumps(
                    {"status": "fail", "failures": ["injected verifier failure"]}
                )
            return real_run(command, cwd=cwd)

        with patch.object(cli_module, "run", side_effect=fail_only_verification):
            with self.assertRaisesRegex(cli_module.InventoryError, "verification failed"):
                cli_module.execute(arguments)

        self.assertEqual(self.bytes_tree(clone), root_before)
        self.assertFalse((clone / RUNTIME_BINDING).exists())
        self.assertEqual(list(runtime.iterdir()), [])
        self.assertTrue(runtime.is_dir())
        self.assertFalse(catalogue.exists())
        self.assertEqual(list(catalogue.parent.iterdir()), [])

    def test_tampered_adoption_catalogue_blocks_all_rollback_deletions(self) -> None:
        clone = self.scratch / "catalogue-tamper-clone"
        shutil.copytree(self.root, clone)
        (clone / RUNTIME_BINDING).unlink()
        runtime = self.scratch / "catalogue-tamper-runtime"
        catalogue = self.scratch / "catalogue-tamper" / "Inventory.md"
        self.assertFalse((runtime / "inventory.sqlite").exists())
        foreign_bytes = b"external catalogue replacement\x00\xff"
        arguments = self.command_for(
            clone,
            runtime,
            self.scratch / "catalogue-tamper-media",
            catalogue,
            "init",
        )[2:]
        real_run = cli_module.run

        def replace_then_fail(command: list[str], *, cwd: Path | None = None) -> str:
            if Path(command[1]).name == "verify.py":
                catalogue.write_bytes(foreign_bytes)
                return json.dumps({"status": "fail", "failures": ["injected"]})
            return real_run(command, cwd=cwd)

        with patch.object(cli_module, "run", side_effect=replace_then_fail):
            with self.assertRaisesRegex(cli_module.InventoryError, "catalogue changed outside"):
                cli_module.execute(arguments)

        self.assertTrue((clone / RUNTIME_BINDING).is_file())
        self.assertTrue((runtime / RUNTIME_OWNER).is_file())
        self.assertFalse((runtime / "inventory.sqlite").exists())
        self.assertEqual(catalogue.read_bytes(), foreign_bytes)

    def test_unexpected_adoption_runtime_byte_blocks_all_rollback_deletions(self) -> None:
        clone = self.scratch / "runtime-tamper-clone"
        shutil.copytree(self.root, clone)
        (clone / RUNTIME_BINDING).unlink()
        runtime = self.scratch / "runtime-tamper-runtime"
        catalogue = self.scratch / "runtime-tamper" / "Inventory.md"
        self.assertFalse((runtime / "inventory.sqlite").exists())
        sentinel = runtime / "foreign-runtime-sentinel"
        sentinel_bytes = b"do not delete\x00\xff"
        journal = cli_module.adoption_rollback_journal_path(runtime)
        environment = dict(os.environ)
        environment["PROPERTY_INVENTORY_FAIL_BEFORE_RENDER_REPLACE"] = "1"
        environment["PROPERTY_INVENTORY_FAIL_DURING_ADOPTION_ROLLBACK"] = "catalogue"
        crashed = subprocess.run(
            self.command_for(
                clone,
                runtime,
                self.scratch / "runtime-tamper-media",
                catalogue,
                "init",
            ),
            text=True,
            capture_output=True,
            check=False,
            env=environment,
        )
        self.assertEqual(crashed.returncode, 97, crashed.stderr or crashed.stdout)
        sentinel.write_bytes(sentinel_bytes)

        blocked = self.execute(
            self.command_for(
                clone,
                runtime,
                self.scratch / "runtime-tamper-media",
                catalogue,
                "init",
            ),
            succeeds=False,
        )

        self.assertIn("runtime changed outside", blocked["error"])
        self.assertTrue((clone / RUNTIME_BINDING).is_file())
        self.assertTrue((runtime / RUNTIME_OWNER).is_file())
        self.assertFalse((runtime / "inventory.sqlite").exists())
        self.assertFalse(catalogue.exists())
        self.assertEqual(sentinel.read_bytes(), sentinel_bytes)
        self.assertTrue(journal.is_file())

        sentinel.unlink()
        recovered = self.execute(
            self.command_for(
                clone,
                runtime,
                self.scratch / "runtime-tamper-media",
                catalogue,
                "init",
            )
        )
        self.assertEqual(recovered["status"], "recovered_initialized")
        self.assertFalse(journal.exists())

    def test_bindingless_migrate_failure_rolls_back_unchanged_generation(self) -> None:
        clone = self.scratch / "migration-failure-clone"
        shutil.copytree(self.root, clone)
        (clone / RUNTIME_BINDING).unlink()
        metadata = clone / "Data" / "store" / "metadata.jsonl"
        metadata.write_text(
            json.dumps(
                {
                    "inventory_id": json.loads(metadata.read_text())["inventory_id"],
                    "schema_version": 99,
                }
            )
            + "\n",
            encoding="utf-8",
        )
        root_before = self.bytes_tree(clone)
        runtime = self.scratch / "migration-failure-runtime"
        runtime.mkdir()
        catalogue = self.scratch / "migration-failure-catalogue" / "Inventory.md"
        catalogue.parent.mkdir()

        failed = self.execute(
            self.command_for(
                clone,
                runtime,
                self.scratch / "migration-failure-media",
                catalogue,
                "migrate",
            ),
            succeeds=False,
        )

        self.assertIn("newer than supported", failed["error"])
        self.assertEqual(self.bytes_tree(clone), root_before)
        self.assertEqual(list(runtime.iterdir()), [])
        self.assertTrue(runtime.is_dir())
        self.assertFalse(catalogue.exists())
        self.assertEqual(list(catalogue.parent.iterdir()), [])

    def test_bindingless_migrate_failure_preserves_bridge_after_generation_changes(self) -> None:
        clone = self.scratch / "migration-changed-clone"
        shutil.copytree(self.root, clone)
        (clone / RUNTIME_BINDING).unlink()
        runtime = self.scratch / "migration-changed-runtime"
        catalogue = self.scratch / "migration-changed-catalogue" / "Inventory.md"
        arguments = self.command_for(
            clone,
            runtime,
            self.scratch / "migration-changed-media",
            catalogue,
            "migrate",
        )[2:]
        metadata = clone / "Data" / "store" / "metadata.jsonl"
        canonical_before = metadata.read_bytes()

        def change_then_fail(_args: object) -> dict:
            metadata.write_bytes(canonical_before + b"\n")
            raise cli_module.InventoryError("injected migration failure")

        with patch.object(cli_module, "command_migrate", side_effect=change_then_fail):
            with self.assertRaisesRegex(cli_module.InventoryError, "injected migration failure"):
                cli_module.execute(arguments)

        self.assertNotEqual(metadata.read_bytes(), canonical_before)
        self.assertEqual(
            json.loads((clone / RUNTIME_BINDING).read_text())["format"], 1
        )
        self.assertTrue((runtime / RUNTIME_OWNER).is_file())
        self.assertFalse(catalogue.exists())

    def test_process_death_during_adoption_rollback_recovers_on_retry(self) -> None:
        clone = self.scratch / "adoption-cleanup-crash-clone"
        shutil.copytree(self.root, clone)
        (clone / RUNTIME_BINDING).unlink()
        runtime = self.scratch / "adoption-cleanup-crash-runtime"
        catalogue = self.scratch / "adoption-cleanup-crash" / "Inventory.md"
        environment = dict(os.environ)
        environment["PROPERTY_INVENTORY_FAIL_BEFORE_RENDER_REPLACE"] = "1"
        environment["PROPERTY_INVENTORY_FAIL_DURING_ADOPTION_ROLLBACK"] = "owner"

        crashed = subprocess.run(
            self.command_for(
                clone,
                runtime,
                self.scratch / "adoption-cleanup-crash-media",
                catalogue,
                "init",
            ),
            text=True,
            capture_output=True,
            check=False,
            env=environment,
        )

        self.assertEqual(crashed.returncode, 97, crashed.stderr or crashed.stdout)
        journal = cli_module.adoption_rollback_journal_path(runtime)
        self.assertTrue(journal.is_file())
        self.assertTrue((clone / RUNTIME_BINDING).is_file())
        self.assertFalse((runtime / RUNTIME_OWNER).exists())

        recovered = self.execute(
            self.command_for(
                clone,
                runtime,
                self.scratch / "adoption-cleanup-crash-media",
                catalogue,
                "init",
            )
        )
        self.assertEqual(recovered["status"], "recovered_initialized")
        self.assertFalse(journal.exists())

    def test_process_death_after_adoption_binding_unlink_recovers_on_retry(self) -> None:
        clone = self.scratch / "adoption-binding-crash-clone"
        shutil.copytree(self.root, clone)
        (clone / RUNTIME_BINDING).unlink()
        runtime = self.scratch / "adoption-binding-crash-runtime"
        catalogue = self.scratch / "adoption-binding-crash" / "Inventory.md"
        environment = dict(os.environ)
        environment["PROPERTY_INVENTORY_FAIL_BEFORE_RENDER_REPLACE"] = "1"
        environment["PROPERTY_INVENTORY_FAIL_DURING_ADOPTION_ROLLBACK"] = "binding"

        crashed = subprocess.run(
            self.command_for(
                clone,
                runtime,
                self.scratch / "adoption-binding-crash-media",
                catalogue,
                "init",
            ),
            text=True,
            capture_output=True,
            check=False,
            env=environment,
        )

        self.assertEqual(crashed.returncode, 97, crashed.stderr or crashed.stdout)
        journal = cli_module.adoption_rollback_journal_path(runtime)
        self.assertTrue(journal.is_file())
        self.assertFalse((clone / RUNTIME_BINDING).exists())

        recovered = self.execute(
            self.command_for(
                clone,
                runtime,
                self.scratch / "adoption-binding-crash-media",
                catalogue,
                "init",
            )
        )
        self.assertEqual(recovered["status"], "recovered_initialized")
        self.assertFalse(journal.exists())

    def test_format_2_root_without_reciprocal_runtime_owner_fails_closed(self) -> None:
        root_before = self.bytes_tree(self.root)
        catalogue_before = self.catalogue.read_bytes()
        (self.runtime / RUNTIME_OWNER).unlink()

        failed = self.execute(self.command("status"), succeeds=False)

        self.assertIn("no reciprocal runtime owner marker", failed["error"])
        self.assertEqual(self.bytes_tree(self.root), root_before)
        self.assertEqual(self.catalogue.read_bytes(), catalogue_before)
        self.assertFalse((self.runtime / RUNTIME_OWNER).exists())

    def test_concurrent_init_on_one_absent_catalogue_has_exactly_one_winner(self) -> None:
        catalogue = self.scratch / "contended-catalogue" / "Inventory.md"
        contenders = [
            (
                self.scratch / f"contender-{number}-root",
                self.scratch / f"contender-{number}-runtime",
                self.scratch / f"contender-{number}-media",
            )
            for number in (1, 2)
        ]
        processes = [
            subprocess.Popen(
                self.command_for(root, runtime, media, catalogue, "init"),
                text=True,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
            )
            for root, runtime, media in contenders
        ]
        results = [process.communicate(timeout=30) for process in processes]
        return_codes = [process.returncode for process in processes]

        self.assertEqual(return_codes.count(0), 1, results)
        winner_index = return_codes.index(0)
        winner_root, winner_runtime, winner_media = contenders[winner_index]
        winner = json.loads(results[winner_index][0])
        self.assertEqual(winner["status"], "initialized")
        catalogue_bytes = catalogue.read_bytes()
        self.assertTrue(catalogue_bytes)
        status = self.execute(
            self.command_for(winner_root, winner_runtime, winner_media, catalogue, "status")
        )
        self.assertEqual(status["verification"]["failures"], [])
        self.assertEqual(catalogue.read_bytes(), catalogue_bytes)

    def test_init_crash_after_root_publication_preserves_unexpected_bytes_and_recovers(
        self,
    ) -> None:
        root = self.scratch / "published-root"
        runtime = self.scratch / "published-runtime"
        media = self.scratch / "published-media"
        catalogue = self.scratch / "published-catalogue" / "Inventory.md"
        environment = dict(os.environ)
        environment["PROPERTY_INVENTORY_FAIL_INIT_AFTER_INSTALL"] = "1"
        crashed = subprocess.run(
            self.command_for(root, runtime, media, catalogue, "init"),
            text=True,
            capture_output=True,
            check=False,
            env=environment,
        )
        self.assertEqual(crashed.returncode, 83, crashed.stderr or crashed.stdout)
        sentinel = root / "unexpected-after-publication.bin"
        sentinel_bytes = b"must survive init recovery\x00\xff"
        sentinel.write_bytes(sentinel_bytes)

        recovered = self.execute(self.command_for(root, runtime, media, catalogue, "init"))

        self.assertEqual(recovered["status"], "recovered_initialized")
        self.assertEqual(sentinel.read_bytes(), sentinel_bytes)
        self.assertEqual(
            self.execute(self.command_for(root, runtime, media, catalogue, "status"))[
                "verification"
            ]["failures"],
            [],
        )

    def test_init_crash_after_owner_claim_recovers_tracked_staging(self) -> None:
        root = self.scratch / "owner-crash-root"
        runtime = self.scratch / "owner-crash-runtime"
        media = self.scratch / "owner-crash-media"
        catalogue = self.scratch / "owner-crash-catalogue" / "Inventory.md"
        environment = dict(os.environ)
        environment["PROPERTY_INVENTORY_FAIL_INIT_AFTER_OWNER"] = "1"

        crashed = subprocess.run(
            self.command_for(root, runtime, media, catalogue, "init"),
            text=True,
            capture_output=True,
            check=False,
            env=environment,
        )

        self.assertEqual(crashed.returncode, 82, crashed.stderr or crashed.stdout)
        self.assertFalse(root.exists())
        self.assertTrue((runtime / ".property-inventory-init.json").is_file())
        recovered = self.execute(self.command_for(root, runtime, media, catalogue, "init"))
        self.assertEqual(recovered["status"], "recovered_initialized")
        self.assertFalse((runtime / ".property-inventory-init.json").exists())
        self.assertEqual(
            list(root.parent.glob(f".{root.name}-init-*")),
            [],
        )

    def test_process_death_during_extraction_leaves_durable_journal_and_retry_recovers(
        self,
    ) -> None:
        # A large authenticated media object gives the restore process enough time to be
        # killed after extraction begins. The journal must already make those bytes resumable.
        self.cli(
            "add-location", "--name", "Crash recovery location", "--location-id", "loc-crash-recovery",
            "--kind", "room",
        )
        item_id = self.cli(
            "order",
            "--actor",
            "Recovery test",
            "--source-ref",
            "large fixture",
            "--name",
            "large recovery fixture",
            "--category",
            "test fixture",
            "--ordered-on",
            "2026-08-05",
            "--order-placed",
        )["result"]["item_id"]
        evidence_id = self.cli(
            "receive",
            "--actor",
            "Recovery test",
            "--source-ref",
            "large receipt",
            "--item-id",
            item_id,
            "--received-on",
            "2026-08-06",
            "--location-id",
            "loc-crash-recovery",
            "--physical-check",
        )["result"]["evidence_id"]
        large_media = self.scratch / "large-recovery-media.bin"
        large_media.write_bytes(b"x" * (64 * 1024 * 1024))
        attached = self.cli(
            "attach-media",
            "--evidence-id",
            evidence_id,
            "--file",
            str(large_media),
            "--role",
            "source",
            "--captured-on",
            "2026-08-06",
            "--media-type",
            "application/octet-stream",
        )
        archive = self.source_archive()
        root = self.scratch / "extraction-root"
        runtime = self.scratch / "extraction-runtime"
        media = self.scratch / "extraction-media"
        catalogue = self.scratch / "extraction-catalogue" / "Inventory.md"
        process = subprocess.Popen(
            self.command_for(root, runtime, media, catalogue, "restore", "--archive", str(archive)),
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        )
        digest = attached["result"]["sha256"]
        deadline = time.monotonic() + 30
        while time.monotonic() < deadline and process.poll() is None:
            if any(
                candidate.name == digest
                for workspace in runtime.glob(".property-inventory-restore-*")
                for candidate in workspace.rglob(digest)
            ):
                process.kill()
                break
            time.sleep(0.01)
        stdout, stderr = process.communicate(timeout=30)
        self.assertIsNotNone(process.returncode, stdout or stderr)
        self.assertNotEqual(process.returncode, 0, stdout or stderr)
        journal_path = runtime / RESTORE_JOURNAL
        self.assertTrue(
            journal_path.is_file(), "extraction must be journaled before private bytes exist"
        )
        journal = json.loads(journal_path.read_text())
        self.assertEqual(journal["phase"], "extracting")

        recovered = self.execute(
            self.command_for(root, runtime, media, catalogue, "restore", "--archive", str(archive))
        )
        self.assertEqual(recovered["status"], "recovered_restored")
        self.assertEqual(recovered["checks"]["verification"]["failures"], [])

    def test_corrupt_archive_does_not_poison_runtime_for_a_valid_retry(self) -> None:
        archive = self.source_archive()
        corrupt = self.scratch / "corrupt.tar.gz"
        corrupt.write_bytes(archive.read_bytes()[:100])
        root = self.scratch / "corrupt-root"
        runtime = self.scratch / "corrupt-runtime"
        media = self.scratch / "corrupt-media"
        catalogue = self.scratch / "corrupt-catalogue" / "Inventory.md"

        failed = subprocess.run(
            self.command_for(root, runtime, media, catalogue, "restore", "--archive", str(corrupt)),
            text=True,
            capture_output=True,
            check=False,
        )
        self.assertNotEqual(failed.returncode, 0, failed.stdout)
        self.assertFalse((runtime / RESTORE_JOURNAL).exists())
        self.assertFalse(root.exists())
        self.assertFalse(media.exists())

        restored = self.execute(
            self.command_for(root, runtime, media, catalogue, "restore", "--archive", str(archive))
        )
        self.assertEqual(restored["status"], "restored")
        self.assertEqual(restored["checks"]["verification"]["failures"], [])

    def test_restore_rollback_preserves_existing_empty_root_modes(self) -> None:
        archive = self.source_archive()
        root = self.scratch / "mode-root"
        runtime = self.scratch / "mode-runtime"
        media = self.scratch / "mode-media"
        catalogue = self.scratch / "mode-catalogue" / "Inventory.md"
        root.mkdir(mode=0o700)
        media.mkdir(mode=0o700)
        root.chmod(0o700)
        media.chmod(0o700)
        environment = dict(os.environ)
        environment["PROPERTY_INVENTORY_FAIL_RESTORE_AFTER_CATALOGUE_REPLACE"] = "1"

        failed = subprocess.run(
            self.command_for(root, runtime, media, catalogue, "restore", "--archive", str(archive)),
            text=True,
            capture_output=True,
            check=False,
            env=environment,
        )

        self.assertNotEqual(failed.returncode, 0, failed.stdout)
        self.assertEqual(root.stat().st_mode & 0o7777, 0o700)
        self.assertEqual(media.stat().st_mode & 0o7777, 0o700)
        self.assertEqual(list(root.iterdir()), [])
        self.assertEqual(list(media.iterdir()), [])

    def test_restore_preserves_non_prefix_bytes_in_interrupted_transfer(self) -> None:
        archive = self.source_archive()
        root = self.scratch / "non-prefix-root"
        runtime = self.scratch / "non-prefix-runtime"
        media = self.scratch / "non-prefix-media"
        catalogue = self.scratch / "non-prefix-catalogue" / "Inventory.md"
        environment = dict(os.environ)
        environment["PROPERTY_INVENTORY_FAIL_AFTER_RESTORE_TRANSFER"] = "inventory"
        crashed = subprocess.run(
            self.command_for(root, runtime, media, catalogue, "restore", "--archive", str(archive)),
            text=True,
            capture_output=True,
            check=False,
            env=environment,
        )
        self.assertEqual(crashed.returncode, 85, crashed.stderr or crashed.stdout)
        journal = json.loads((runtime / RESTORE_JOURNAL).read_text())
        token = hashlib.sha256(journal["restore_id"].encode()).hexdigest()[:16]
        transfer = root.with_name(f".{root.name}.property-inventory-transfer-inventory-{token}")
        build = transfer.with_name(transfer.name + ".building")
        transfer.rename(build)
        changed = next(path for path in build.rglob("*") if path.is_file())
        sentinel = b"not a source prefix\x00\xff"
        changed.write_bytes(sentinel)

        failed = self.execute(
            self.command_for(root, runtime, media, catalogue, "restore", "--archive", str(archive)),
            succeeds=False,
        )

        self.assertIn("not a source prefix", failed["error"])
        self.assertEqual(changed.read_bytes(), sentinel)

    def test_concurrent_shared_media_restores_cannot_delete_winner_bytes(self) -> None:
        archive = self.source_archive_with_media()
        shared_media = self.scratch / "shared-restored-media"
        catalogue_one = self.scratch / "shared-media-catalogue-one" / "Inventory.md"
        catalogue_two = self.scratch / "shared-media-catalogue-two" / "Inventory.md"
        contenders = [
            (
                self.scratch / "shared-media-root-one",
                self.scratch / "shared-media-runtime-one",
                catalogue_one,
            ),
            (
                self.scratch / "shared-media-root-two",
                self.scratch / "shared-media-runtime-two",
                catalogue_two,
            ),
        ]
        processes = [
            subprocess.Popen(
                self.command_for(
                    root, runtime, shared_media, catalogue, "restore", "--archive", str(archive)
                ),
                text=True,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
            )
            for root, runtime, catalogue in contenders
        ]
        results = [process.communicate(timeout=30) for process in processes]
        return_codes = [process.returncode for process in processes]

        self.assertEqual(return_codes.count(0), 1, results)
        winner_index = return_codes.index(0)
        winner_root, winner_runtime, winner_catalogue = contenders[winner_index]
        winner_media = self.bytes_tree(shared_media)
        self.assertTrue(winner_media)
        self.assertEqual(
            self.execute(
                self.command_for(
                    winner_root, winner_runtime, shared_media, winner_catalogue, "status"
                )
            )["verification"]["failures"],
            [],
        )
        self.assertEqual(self.bytes_tree(shared_media), winner_media)

    def test_restored_clones_share_inventory_identity_but_not_catalogue_owner(self) -> None:
        archive = self.source_archive()
        shared_catalogue = self.scratch / "clone-catalogue" / "Inventory.md"
        first = (
            self.scratch / "clone-one-root",
            self.scratch / "clone-one-runtime",
            self.scratch / "clone-one-media",
        )
        second = (
            self.scratch / "clone-two-root",
            self.scratch / "clone-two-runtime",
            self.scratch / "clone-two-media",
        )
        first_result = self.execute(
            self.command_for(
                *first,
                shared_catalogue,
                "restore",
                "--archive",
                str(archive),
            )
        )
        catalogue_before = shared_catalogue.read_bytes()

        second_result = self.execute(
            self.command_for(
                *second,
                shared_catalogue,
                "restore",
                "--archive",
                str(archive),
            ),
            succeeds=False,
        )

        self.assertIn("different inventory owner", second_result["error"])
        self.assertEqual(shared_catalogue.read_bytes(), catalogue_before)
        self.assertEqual(
            self.execute(self.command_for(*first, shared_catalogue, "status"))["verification"][
                "failures"
            ],
            [],
        )
        self.assertEqual(
            first_result["inventory_id"],
            json.loads((self.root / "Data/store/metadata.jsonl").read_text())["inventory_id"],
        )
        self.assertNotEqual(
            self.installation_id(first[0]),
            json.loads((second[1] / RUNTIME_OWNER).read_text())["installation_id"],
        )


if __name__ == "__main__":
    unittest.main()
