#!/usr/bin/env python3
"""Pure-core acceptance tests for ecosystem import, doctor, and compatibility."""

from __future__ import annotations

import json
import os
import tempfile
import unittest
from dataclasses import replace
from pathlib import Path
from unittest.mock import patch

import property_inventory.compatibility_policy as compatibility_policy
from property_inventory.cli import InventoryError, read_bounded_regular_input
from property_inventory.compatibility_policy import (
    CompatibilityError,
    compatibility_matrix,
    validate_migration,
    validate_runtime,
    validate_schema,
)
from property_inventory.doctor import (
    CommandResult,
    DoctorCommand,
    DoctorError,
    plan_blank_restore,
    run_blank_restore,
)
from property_inventory.importers import ImportError, normalize_import

HERE = Path(__file__).resolve().parents[1]
FIXTURES = HERE / "tests" / "fixtures" / "imports"


def option(operation: tuple[str, ...], flag: str) -> str | None:
    try:
        return operation[operation.index(flag) + 1]
    except ValueError:
        return None


class ImportNormalizationTest(unittest.TestCase):
    def test_cart_and_order_have_distinct_non_possession_semantics(self) -> None:
        proposal = normalize_import(
            (FIXTURES / "cart-and-order.csv").read_bytes(),
            source_format="csv",
            source_name="cart-and-order.csv",
            source_namespace="shop-a",
            imported_on="2026-08-05",
        )
        cart, order = proposal.operations
        self.assertEqual(cart[0], "plan")
        self.assertNotIn("--order-placed", cart)
        self.assertEqual(order[0], "order")
        self.assertIn("--order-placed", order)
        for operation in proposal.operations:
            self.assertNotIn("receive", operation)
            self.assertNotIn("physical-check", operation)
            self.assertNotIn("--location-id", operation)
        self.assertEqual(proposal.operation_lists(), [list(cart), list(order)])

    def test_source_provenance_unknowns_and_formula_are_preserved_as_data(self) -> None:
        payload = (FIXTURES / "formula.json").read_bytes()
        proposal = normalize_import(
            payload,
            source_format="json",
            source_name="formula.json",
            source_namespace="shop-a",
            imported_on="2026-08-05",
        )
        operation = proposal.operations[0]
        self.assertEqual(option(operation, "--name"), '=HYPERLINK("https://invalid.test","unsafe formula")')
        self.assertEqual(json.loads(option(operation, "--specs") or "{}"), {})
        provenance = json.loads(option(operation, "--notes") or "{}")["generic_import"]
        self.assertEqual(provenance["raw_fields"]["notes"], "+SUM(1,1)")
        self.assertIsNone(provenance["raw_fields"]["quantity"])
        self.assertIsNone(provenance["raw_fields"]["condition"])
        self.assertIsNone(provenance["raw_fields"]["current_location"])
        self.assertNotIn("--quantity", operation)
        self.assertNotIn("--location-id", operation)
        self.assertEqual(provenance["import"]["source_sha256"], proposal.source_sha256)
        self.assertEqual(provenance["import"]["source_namespace"], "shop-a")
        self.assertIn("sha256=", option(operation, "--source-ref") or "")
        self.assertEqual(provenance["import"]["format"], "json")

    def test_deterministic_duplicate_and_collision_rejection(self) -> None:
        payload = (FIXTURES / "cart-and-order.csv").read_bytes()
        first = normalize_import(
            payload, source_format="csv", source_name="cart-and-order.csv", source_namespace="shop-a", imported_on="2026-08-05"
        )
        second = normalize_import(
            payload, source_format="csv", source_name="cart-and-order.csv", source_namespace="shop-a", imported_on="2026-08-05"
        )
        self.assertEqual(first, second)
        with self.assertRaisesRegex(ImportError, "collides with an existing"):
            normalize_import(
                payload,
                source_format="csv",
                source_name="cart-and-order.csv",
                source_namespace="shop-a",
                imported_on="2026-08-05",
                known_external_keys=(("shop-a", "order-1"),),
            )
        renamed = normalize_import(
            payload,
            source_format="csv",
            source_name="renamed-export.csv",
            source_namespace="shop-b",
            imported_on="2026-08-05",
            known_external_keys=(("shop-a", "order-1"),),
        )
        self.assertEqual(len(renamed.records), 2)
        duplicate = b"status,name,date\ncart,Same,2026-08-01\ncart,Same,2026-08-01\n"
        with self.assertRaisesRegex(ImportError, "duplicates an earlier source row"):
            normalize_import(
                duplicate, source_format="csv", source_name="duplicate.csv", source_namespace="shop-a", imported_on="2026-08-05"
            )

    def test_ambiguous_and_malformed_rows_do_not_become_operations(self) -> None:
        with self.assertRaisesRegex(ImportError, "ambiguous status"):
            normalize_import(
                b"status,name\nreceived,Already there\n",
                source_format="csv",
                source_name="unsafe.csv",
                source_namespace="shop-a",
                imported_on="2026-08-05",
            )

        with self.assertRaisesRegex(ImportError, "headers collide"):
            normalize_import(
                b"name,Name,status\nA,B,cart\n",
                source_format="csv",
                source_name="bad.csv",
                source_namespace="shop-a",
                imported_on="2026-08-05",
            )
        with self.assertRaisesRegex(ImportError, "date is required"):
            normalize_import(
                b"status,name\nordered,Undated source\n",
                source_format="csv",
                source_name="undated.csv",
                source_namespace="shop-a",
                imported_on="2026-08-05",
            )
        with self.assertRaisesRegex(ImportError, "duplicate JSON key"):
            normalize_import(
                b'[{"status":"received","status":"cart","name":"ambiguous","date":"2026-08-05"}]',
                source_format="json",
                source_name="duplicate-key.json",
                source_namespace="shop-a",
                imported_on="2026-08-05",
            )
        with self.assertRaisesRegex(ImportError, "cannot be represented exactly"):
            normalize_import(
                b"status,name,date,quantity\ncart,Huge,2026-08-05,9007199254740993\n",
                source_format="csv",
                source_name="huge.csv",
                source_namespace="shop-a",
                imported_on="2026-08-05",
            )
        with self.assertRaisesRegex(ImportError, "NUL"):
            normalize_import(
                b"status,name,date\ncart,A\x00B,2026-08-05\n",
                source_format="csv",
                source_name="nul.csv",
                source_namespace="shop-a",
                imported_on="2026-08-05",
            )
        with self.assertRaisesRegex(ImportError, "without changing it"):
            normalize_import(
                b'[{"status":"cart","name":"Precise","date":"2026-08-05","quantity":1.0000000000000001}]',
                source_format="json",
                source_name="precision.json",
                source_namespace="shop-a",
                imported_on="2026-08-05",
            )
        deeply_nested = (
            b'[{"status":"cart","name":"Nested","date":"2026-08-05","extra":'
            + b"[" * 10_000
            + b"0"
            + b"]" * 10_000
            + b"}]"
        )
        with self.assertRaisesRegex(ImportError, "malformed JSON"):
            normalize_import(
                deeply_nested,
                source_format="json",
                source_name="nested.json",
                source_namespace="shop-a",
                imported_on="2026-08-05",
            )

    def test_bounded_import_reader_rejects_path_swap_after_open(self) -> None:
        with tempfile.TemporaryDirectory(prefix="inventory-import-reader-") as temporary:
            root = Path(temporary)
            source = root / "source.json"
            replacement = root / "replacement.json"
            opened = root / "opened.json"
            source.write_bytes(b"original")
            replacement.write_bytes(b"replacement")
            real_read = os.read
            swapped = False

            def swapping_read(descriptor: int, byte_count: int) -> bytes:
                nonlocal swapped
                result = real_read(descriptor, byte_count)
                if not swapped:
                    source.rename(opened)
                    replacement.rename(source)
                    swapped = True
                return result

            with patch("property_inventory.cli.os.read", side_effect=swapping_read):
                with self.assertRaisesRegex(InventoryError, "changed while it was read"):
                    read_bounded_regular_input(
                        source, maximum_bytes=1024, label="import input"
                    )


class DoctorTest(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory(prefix="inventory-doctor-")
        self.base = Path(self.temp.name)
        self.plan = plan_blank_restore(
            executable=("property-inventory",),
            source_inventory_root=self.base / "source-inventory",
            source_runtime_dir=self.base / "source-runtime",
            source_media_root=self.base / "source-media",
            archive=self.base / "archives" / "drill.tar.gz",
            restored_inventory_root=self.base / "restored-inventory",
            restored_runtime_dir=self.base / "restored-runtime",
            restored_media_root=self.base / "restored-media",
        )

    def tearDown(self) -> None:
        self.temp.cleanup()

    def test_doctor_requires_export_blank_restore_then_status(self) -> None:
        calls: list[tuple[str, ...]] = []
        checked: list[Path] = []

        def runner(arguments: tuple[str, ...]) -> CommandResult:
            calls.append(arguments)
            if "export" in arguments:
                self.plan.archive.parent.mkdir(parents=True, exist_ok=True)
                self.plan.archive.write_bytes(b"archive")
            stdout = '{"status":"pass"}' if arguments[-1] == "status" else "{}"
            return CommandResult(0, stdout=stdout)

        report = run_blank_restore(
            self.plan,
            runner=runner,
            blank_validator=lambda path: checked.append(path) is None,
        )
        self.assertEqual([command.label for command, _ in report.results], ["export", "restore", "status"])
        self.assertEqual(calls, [command.arguments for command in self.plan.commands])
        self.assertEqual(
            checked,
            [
                self.plan.restored_inventory_root,
                self.plan.restored_runtime_dir,
                self.plan.restored_media_root,
            ]
            * 2,
        )
        self.assertIn("restore", calls[1])
        self.assertIn("status", calls[2])

    def test_doctor_does_not_mask_export_or_restore_failure(self) -> None:
        calls: list[tuple[str, ...]] = []

        def failed_export(arguments: tuple[str, ...]) -> CommandResult:
            calls.append(arguments)
            return CommandResult(23, stderr="export broken")

        with self.assertRaisesRegex(DoctorError, "export failed"):
            run_blank_restore(
                self.plan,
                runner=failed_export,
                blank_validator=lambda path: True,
                archive_validator=lambda path: True,
            )
        self.assertEqual(len(calls), 1)

        def failed_restore(arguments: tuple[str, ...]) -> CommandResult:
            calls.append(arguments)
            return CommandResult(7 if arguments[-2:] == ("--archive", str(self.plan.archive)) else 0, stderr="restore broken")

        with self.assertRaisesRegex(DoctorError, "restore failed"):
            run_blank_restore(
                self.plan,
                runner=failed_restore,
                blank_validator=lambda path: True,
                archive_validator=lambda path: True,
                archive_hasher=lambda path: "fixture-digest",
            )

    def test_doctor_refuses_non_blank_or_overlapping_targets(self) -> None:
        with self.assertRaisesRegex(DoctorError, "not blank"):
            run_blank_restore(
                self.plan,
                runner=lambda arguments: CommandResult(0),
                blank_validator=lambda path: False,
                archive_validator=lambda path: True,
            )
        with self.assertRaisesRegex(DoctorError, "overlaps"):
            plan_blank_restore(
                executable=("property-inventory",),
                source_inventory_root=self.base / "same",
                source_runtime_dir=self.base / "source-runtime",
                source_media_root=self.base / "source-media",
                archive=self.base / "archive.tar.gz",
                restored_inventory_root=self.base / "same",
                restored_runtime_dir=self.base / "restored-runtime",
                restored_media_root=self.base / "restored-media",
            )
        source = self.base / "source-via-link"
        source.mkdir()
        alias = self.base / "source-alias"
        alias.symlink_to(source, target_is_directory=True)
        with self.assertRaisesRegex(DoctorError, "overlaps"):
            plan_blank_restore(
                executable=("property-inventory",),
                source_inventory_root=source,
                source_runtime_dir=self.base / "source-runtime-2",
                source_media_root=self.base / "source-media-2",
                archive=self.base / "archive-2.tar.gz",
                restored_inventory_root=alias / "restored-child",
                restored_runtime_dir=self.base / "restored-runtime-2",
                restored_media_root=self.base / "restored-media-2",
            )

    def test_doctor_revalidates_filesystem_aliases_in_directly_constructed_plans(self) -> None:
        alias = self.base / "source-inventory-alias"
        alias.symlink_to(self.plan.source_inventory_root, target_is_directory=True)
        restore_arguments = list(self.plan.commands[1].arguments)
        restore_arguments[restore_arguments.index("--inventory-root") + 1] = str(alias)
        status_arguments = list(self.plan.commands[2].arguments)
        status_arguments[status_arguments.index("--inventory-root") + 1] = str(alias)
        forged = replace(
            self.plan,
            restored_inventory_root=alias,
            commands=(
                self.plan.commands[0],
                DoctorCommand("restore", tuple(restore_arguments)),
                DoctorCommand("status", tuple(status_arguments)),
            ),
        )
        with self.assertRaisesRegex(DoctorError, "overlaps"):
            run_blank_restore(forged, runner=lambda arguments: CommandResult(0))

    def test_doctor_rejects_fake_plan_target_race_and_failed_status(self) -> None:
        with self.assertRaisesRegex(DoctorError, "exactly match"):
            run_blank_restore(
                replace(self.plan, commands=(self.plan.commands[-1],)),
                runner=lambda arguments: CommandResult(0, stdout='{"status":"pass"}'),
            )

        checks = 0

        def target_changes_after_export(path: Path) -> bool:
            nonlocal checks
            checks += 1
            return checks <= 3

        with self.assertRaisesRegex(DoctorError, "changed after export"):
            run_blank_restore(
                self.plan,
                runner=lambda arguments: CommandResult(0, stdout="{}"),
                blank_validator=target_changes_after_export,
                archive_validator=lambda path: True,
                archive_hasher=lambda path: "fixture-digest",
            )

        def failed_status(arguments: tuple[str, ...]) -> CommandResult:
            if "export" in arguments:
                self.plan.archive.parent.mkdir(parents=True, exist_ok=True)
                self.plan.archive.write_bytes(b"archive")
            return CommandResult(0, stdout='{"status":"fail"}')

        with self.assertRaisesRegex(DoctorError, "restored status did not pass"):
            run_blank_restore(
                self.plan,
                runner=failed_status,
                blank_validator=lambda path: True,
            )

    def test_doctor_binds_source_executable_archive_and_strict_status(self) -> None:
        with self.assertRaisesRegex(DoctorError, "executable"):
            plan_blank_restore(
                executable="property-inventory",  # type: ignore[arg-type]
                source_inventory_root=self.base / "source-inventory",
                source_runtime_dir=self.base / "source-runtime",
                source_media_root=self.base / "source-media",
                archive=self.base / "archive.tar.gz",
                restored_inventory_root=self.base / "restored-inventory-2",
                restored_runtime_dir=self.base / "restored-runtime-2",
                restored_media_root=self.base / "restored-media-2",
            )

        export = self.plan.commands[0]
        arguments = list(export.arguments)
        arguments[arguments.index("--inventory-root") + 1] = str(self.base / "wrong-source")
        forged = replace(
            self.plan,
            commands=(DoctorCommand("export", tuple(arguments)), *self.plan.commands[1:]),
        )
        with self.assertRaisesRegex(DoctorError, "exactly match"):
            run_blank_restore(
                forged,
                runner=lambda arguments: CommandResult(0, stdout='{"status":"pass"}'),
            )

        checks = 0

        def substitute_after_export(path: Path) -> bool:
            nonlocal checks
            checks += 1
            if checks == 4:
                self.plan.archive.write_bytes(b"substituted archive")
            return True

        def export_original(arguments: tuple[str, ...]) -> CommandResult:
            if "export" in arguments:
                self.plan.archive.parent.mkdir(parents=True, exist_ok=True)
                self.plan.archive.write_bytes(b"original archive")
            return CommandResult(0, stdout='{"status":"pass"}')

        with self.assertRaisesRegex(DoctorError, "changed before restore"):
            run_blank_restore(
                self.plan,
                runner=export_original,
                blank_validator=substitute_after_export,
            )

        def replace_archive_during_restore(arguments: tuple[str, ...]) -> CommandResult:
            if "export" in arguments:
                self.plan.archive.parent.mkdir(parents=True, exist_ok=True)
                self.plan.archive.write_bytes(b"original archive")
            elif "restore" in arguments:
                self.plan.archive.write_bytes(b"replacement archive")
            return CommandResult(0, stdout='{"status":"pass"}')

        with self.assertRaisesRegex(DoctorError, "changed during restore"):
            run_blank_restore(
                self.plan,
                runner=replace_archive_during_restore,
                blank_validator=lambda path: True,
            )

        def duplicate_status(arguments: tuple[str, ...]) -> CommandResult:
            if "export" in arguments:
                self.plan.archive.parent.mkdir(parents=True, exist_ok=True)
                self.plan.archive.write_bytes(b"archive")
            stdout = '{"status":"fail","status":"pass"}' if arguments[-1] == "status" else "{}"
            return CommandResult(0, stdout=stdout)

        with self.assertRaisesRegex(DoctorError, "did not return JSON"):
            run_blank_restore(
                self.plan,
                runner=duplicate_status,
                blank_validator=lambda path: True,
            )


class CompatibilityPolicyTest(unittest.TestCase):
    def test_matrix_accepts_current_and_supported_migrations(self) -> None:
        matrix = compatibility_matrix((3, 11))
        self.assertEqual(matrix.current_schema_version, 6)
        self.assertEqual(
            [entry.schema_version for entry in matrix.entries], [1, 2, 3, 4, 5, 6]
        )
        self.assertEqual(matrix.entry_for(4).action, "migrate_v4_to_v6")
        self.assertEqual(
            validate_migration(1, python_version=(3, 11)).action,
            "migrate_v1_to_v6",
        )
        self.assertEqual(validate_migration(6, python_version=(3, 11)).action, "read_current")

    def test_policy_rejects_unsupported_schemas_targets_and_python(self) -> None:
        for version in (0, 7, True, "6"):
            with self.assertRaises(CompatibilityError):
                validate_schema(version)
        with self.assertRaises(CompatibilityError):
            validate_migration(1, 3, python_version=(3, 11))
        with self.assertRaisesRegex(CompatibilityError, "requires >= 3.11"):
            validate_runtime((3, 10))
        self.assertFalse(any(entry.supported for entry in compatibility_matrix((3, 10)).entries))
        with patch.object(compatibility_policy, "SCHEMA_VERSION", 7):
            with self.assertRaisesRegex(CompatibilityError, "explicitly updated"):
                compatibility_policy.compatibility_matrix((3, 11))
            with self.assertRaisesRegex(CompatibilityError, "explicitly updated"):
                validate_schema(6)


if __name__ == "__main__":
    unittest.main()
