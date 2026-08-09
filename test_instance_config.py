"""Focused safety tests for independent inventory instance configuration."""

from __future__ import annotations

import json
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parent / "src"))

from property_inventory.config import ConfigError, default_config_path, load_instance_config


class InstanceConfigTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary_directory = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary_directory.name)
        self.values = {
            "inventory_root": str(self.root / "inventory"),
            "runtime_dir": str(self.root / "runtime"),
            "media_root": str(self.root / "media"),
            "catalogue_output": str(self.root / "vault" / "Inventory.md"),
            "catalogue_scope": "private",
            "forbidden_roots": [str(self.root / "vault")],
        }

    def tearDown(self) -> None:
        self.temporary_directory.cleanup()

    def config_file(self, values: dict[str, object] | None = None) -> Path:
        path = self.root / "config.json"
        path.write_text(json.dumps(values if values is not None else self.values), encoding="utf-8")
        return path

    def test_private_topology_resolves_to_absolute_paths(self) -> None:
        config = load_instance_config(config_path=self.config_file())

        self.assertEqual(config.inventory_root, (self.root / "inventory").resolve())
        self.assertEqual(config.runtime_dir, (self.root / "runtime").resolve())
        self.assertEqual(config.media_root, (self.root / "media").resolve())
        self.assertEqual(config.catalogue_output, (self.root / "vault" / "Inventory.md").resolve())
        self.assertEqual(config.catalogue_scope, "private")
        self.assertEqual(config.forbidden_roots, ((self.root / "vault").resolve(),))

    def test_default_path_uses_application_support(self) -> None:
        self.assertEqual(
            default_config_path(home=Path("/fixture/account")),
            Path("/fixture/account/Library/Application Support/property-inventory/config.json"),
        )

    def test_invalid_utf8_config_is_a_configuration_error(self) -> None:
        path = self.root / "invalid-utf8.json"
        path.write_bytes(b"\xff\xfe\x00")

        with self.assertRaisesRegex(ConfigError, "cannot read config file"):
            load_instance_config(config_path=path)

    def test_cli_then_environment_then_config_precedence(self) -> None:
        environment = {
            "PROPERTY_INVENTORY_ROOT": str(self.root / "environment-inventory"),
            "PROPERTY_INVENTORY_RUNTIME": str(self.root / "environment-runtime"),
            "PROPERTY_INVENTORY_MEDIA_ROOT": str(self.root / "environment-media"),
            "PROPERTY_INVENTORY_CATALOGUE_OUTPUT": str(self.root / "environment-vault" / "Out.md"),
            "PROPERTY_INVENTORY_CATALOGUE_SCOPE": "personal",
            "PROPERTY_INVENTORY_FORBIDDEN_ROOTS": str(self.root / "environment-vault"),
        }
        config = load_instance_config(
            config_path=self.config_file(),
            inventory_root=self.root / "cli-inventory",
            runtime_dir=self.root / "cli-runtime",
            media_root=self.root / "cli-media",
            catalogue_output=self.root / "cli-vault" / "Out.md",
            catalogue_scope="public",
            forbidden_roots=[self.root / "cli-vault"],
            environ=environment,
        )

        self.assertEqual(config.inventory_root, (self.root / "cli-inventory").resolve())
        self.assertEqual(config.runtime_dir, (self.root / "cli-runtime").resolve())
        self.assertEqual(config.media_root, (self.root / "cli-media").resolve())
        self.assertEqual(config.catalogue_output, (self.root / "cli-vault" / "Out.md").resolve())
        self.assertEqual(config.catalogue_scope, "public")
        self.assertEqual(
            config.forbidden_roots,
            (
                (self.root / "vault").resolve(),
                (self.root / "environment-vault").resolve(),
                (self.root / "cli-vault").resolve(),
            ),
        )

    def test_environment_overrides_config(self) -> None:
        environment = {
            "PROPERTY_INVENTORY_ROOT": str(self.root / "environment-inventory"),
            "PROPERTY_INVENTORY_RUNTIME": str(self.root / "environment-runtime"),
            "PROPERTY_INVENTORY_MEDIA_ROOT": str(self.root / "environment-media"),
            "PROPERTY_INVENTORY_CATALOGUE_OUTPUT": str(self.root / "environment-vault" / "Out.md"),
            "PROPERTY_INVENTORY_CATALOGUE_SCOPE": "personal",
        }
        config = load_instance_config(config_path=self.config_file(), environ=environment)

        self.assertEqual(config.catalogue_scope, "personal")
        self.assertEqual(config.catalogue_output, (self.root / "environment-vault" / "Out.md").resolve())

    def test_partial_override_loads_default_config(self) -> None:
        config_path = self.config_file()
        override = self.root / "other-vault" / "Out.md"

        with patch("property_inventory.config.default_config_path", return_value=config_path):
            config = load_instance_config(catalogue_output=override, environ={})

        self.assertEqual(config.inventory_root, (self.root / "inventory").resolve())
        self.assertEqual(config.runtime_dir, (self.root / "runtime").resolve())
        self.assertEqual(config.media_root, (self.root / "media").resolve())
        self.assertEqual(config.catalogue_output, override.resolve())
        self.assertEqual(config.forbidden_roots, ((self.root / "vault").resolve(),))

    def test_legacy_explicit_root_and_runtime_remain_standalone(self) -> None:
        config_path = self.config_file()
        standalone_inventory = self.root / "standalone-inventory"
        standalone_runtime = self.root / "standalone-runtime"

        with patch("property_inventory.config.default_config_path", return_value=config_path):
            config = load_instance_config(
                inventory_root=standalone_inventory,
                runtime_dir=standalone_runtime,
                environ={},
            )

        self.assertEqual(config.inventory_root, standalone_inventory.resolve())
        self.assertEqual(config.runtime_dir, standalone_runtime.resolve())
        self.assertIsNone(config.media_root)
        self.assertEqual(
            config.catalogue_output,
            (standalone_inventory / "Inventory.md").resolve(),
        )
        self.assertEqual(config.forbidden_roots, ((self.root / "vault").resolve(),))

    def test_legacy_standalone_paths_cannot_bypass_default_forbidden_roots(self) -> None:
        config_path = self.config_file()

        with patch("property_inventory.config.default_config_path", return_value=config_path):
            with self.assertRaisesRegex(ConfigError, "inventory_root must not overlap"):
                load_instance_config(
                    inventory_root=self.root / "vault" / "unsafe-inventory",
                    runtime_dir=self.root / "standalone-runtime",
                    environ={},
                )

    def test_all_instance_root_pairs_reject_overlap(self) -> None:
        names = ("inventory_root", "runtime_dir", "media_root", "catalogue_output")
        for index, left in enumerate(names):
            for right in names[index + 1 :]:
                with self.subTest(left=left, right=right):
                    values = dict(self.values)
                    values[right] = str(self.root / "shared" / "child")
                    values[left] = str(self.root / "shared")
                    values["forbidden_roots"] = []
                    with self.assertRaisesRegex(ConfigError, f"{left} and {right}"):
                        load_instance_config(config_path=self.config_file(values))

    def test_each_data_root_rejects_forbidden_root_overlap(self) -> None:
        for name in ("inventory_root", "runtime_dir", "media_root"):
            with self.subTest(name=name):
                values = dict(self.values)
                values[name] = str(self.root / "forbidden" / "child")
                values["forbidden_roots"] = [str(self.root / "forbidden")]
                with self.assertRaisesRegex(ConfigError, f"{name} must not overlap"):
                    load_instance_config(config_path=self.config_file(values))

    def test_data_root_alias_inside_forbidden_root_is_rejected_lexically(self) -> None:
        forbidden_root = self.root / "forbidden"
        escaped_root = self.root / "escaped"
        forbidden_root.mkdir()
        escaped_root.mkdir()
        alias = forbidden_root / "alias"
        alias.symlink_to(escaped_root, target_is_directory=True)

        for name in ("inventory_root", "runtime_dir", "media_root"):
            with self.subTest(name=name):
                values = dict(self.values)
                values[name] = str(alias / name)
                values["forbidden_roots"] = [str(forbidden_root)]

                with self.assertRaisesRegex(ConfigError, f"{name} must not overlap"):
                    load_instance_config(config_path=self.config_file(values))

    def test_data_root_alias_outside_forbidden_root_is_permitted(self) -> None:
        physical_root = self.root / "physical"
        physical_root.mkdir()
        alias = self.root / "system-alias"
        alias.symlink_to(physical_root, target_is_directory=True)
        values = dict(self.values)
        values["inventory_root"] = str(alias / "inventory")
        values["forbidden_roots"] = [str(physical_root / "forbidden")]

        config = load_instance_config(config_path=self.config_file(values))

        self.assertEqual(config.inventory_root, (physical_root / "inventory").resolve())

    def test_resolution_failures_raise_configuration_errors(self) -> None:
        for name in (
            "inventory_root",
            "runtime_dir",
            "media_root",
            "catalogue_output",
        ):
            with self.subTest(name=name):
                loop = self.root / f"{name}-loop"
                loop.symlink_to(loop)
                values = dict(self.values)
                values[name] = str(loop)

                with self.assertRaisesRegex(ConfigError, f"cannot resolve {name}"):
                    load_instance_config(config_path=self.config_file(values))

        forbidden_loop = self.root / "forbidden-loop"
        forbidden_loop.symlink_to(forbidden_loop)
        values = dict(self.values)
        values["forbidden_roots"] = [str(forbidden_loop)]
        with self.assertRaisesRegex(ConfigError, "cannot resolve forbidden_roots"):
            load_instance_config(config_path=self.config_file(values))

        config_loop = self.root / "config-loop"
        config_loop.symlink_to(config_loop)
        with self.assertRaisesRegex(ConfigError, "cannot resolve config_path"):
            load_instance_config(config_path=config_loop)

    def test_catalogue_can_be_inside_forbidden_root_but_not_equal_or_its_parent(self) -> None:
        values = dict(self.values)
        values["catalogue_output"] = str(self.root / "forbidden")
        values["forbidden_roots"] = [str(self.root / "forbidden")]
        with self.assertRaisesRegex(ConfigError, "catalogue_output may be inside"):
            load_instance_config(config_path=self.config_file(values))

        values["catalogue_output"] = str(self.root / "catalogue")
        values["forbidden_roots"] = [str(self.root / "catalogue" / "child")]
        with self.assertRaisesRegex(ConfigError, "catalogue_output may be inside"):
            load_instance_config(config_path=self.config_file(values))

    def test_invalid_catalogue_scope_is_rejected(self) -> None:
        values = dict(self.values)
        values["catalogue_scope"] = "team"
        with self.assertRaisesRegex(ConfigError, "catalogue_scope must be one of"):
            load_instance_config(config_path=self.config_file(values))

    def test_catalogue_cannot_overwrite_its_instance_config(self) -> None:
        path = self.root / "self-overwriting-config.json"
        values = dict(self.values)
        values["catalogue_output"] = str(path)
        path.write_text(json.dumps(values), encoding="utf-8")

        with self.assertRaisesRegex(ConfigError, "must not overwrite"):
            load_instance_config(config_path=path)

    def test_catalogue_rejects_symlink_leaf_without_touching_target(self) -> None:
        sentinel = self.root / "external-sentinel.md"
        sentinel.write_bytes(b"external sentinel")
        catalogue_link = self.root / "catalogue-link.md"
        catalogue_link.symlink_to(sentinel)
        values = dict(self.values)
        values["catalogue_output"] = str(catalogue_link)

        with self.assertRaisesRegex(ConfigError, "catalogue_output must not be a symlink"):
            load_instance_config(config_path=self.config_file(values))

        self.assertEqual(sentinel.read_bytes(), b"external sentinel")

    def test_catalogue_rejects_symlink_parent_without_touching_target(self) -> None:
        external_directory = self.root / "external-catalogue"
        external_directory.mkdir()
        sentinel = external_directory / "sentinel"
        sentinel.write_bytes(b"external sentinel")
        catalogue_directory_link = self.root / "catalogue-link"
        catalogue_directory_link.symlink_to(external_directory, target_is_directory=True)
        values = dict(self.values)
        values["catalogue_output"] = str(catalogue_directory_link / "Inventory.md")

        with self.assertRaisesRegex(
            ConfigError, "catalogue_output parent must not be a symlink"
        ):
            load_instance_config(config_path=self.config_file(values))

        self.assertEqual(sentinel.read_bytes(), b"external sentinel")

    def test_catalogue_does_not_scan_non_immediate_symlink_ancestors(self) -> None:
        external_directory = self.root / "external-catalogue"
        catalogue_directory = external_directory / "catalogue"
        catalogue_directory.mkdir(parents=True)
        ancestor_link = self.root / "ancestor-link"
        ancestor_link.symlink_to(external_directory, target_is_directory=True)
        values = dict(self.values)
        values["catalogue_output"] = str(ancestor_link / "catalogue" / "Inventory.md")

        config = load_instance_config(config_path=self.config_file(values))

        self.assertEqual(
            config.catalogue_output, (catalogue_directory / "Inventory.md").resolve()
        )

    def test_catalogue_overlap_uses_resolved_path_after_lexical_symlink_check(self) -> None:
        external_directory = self.root / "external-inventory"
        catalogue_directory = external_directory / "catalogue"
        catalogue_directory.mkdir(parents=True)
        ancestor_link = self.root / "ancestor-link"
        ancestor_link.symlink_to(external_directory, target_is_directory=True)
        values = dict(self.values)
        values["inventory_root"] = str(external_directory)
        values["catalogue_output"] = str(ancestor_link / "catalogue" / "Inventory.md")
        values["forbidden_roots"] = []

        with self.assertRaisesRegex(ConfigError, "inventory_root and catalogue_output"):
            load_instance_config(config_path=self.config_file(values))

    def test_catalogue_forbidden_root_check_uses_resolved_path_after_lexical_symlink_check(
        self,
    ) -> None:
        external_directory = self.root / "external-catalogue"
        catalogue_directory = external_directory / "catalogue"
        catalogue_directory.mkdir(parents=True)
        ancestor_link = self.root / "ancestor-link"
        ancestor_link.symlink_to(external_directory, target_is_directory=True)
        values = dict(self.values)
        values["catalogue_output"] = str(ancestor_link / "catalogue" / "Inventory.md")
        values["forbidden_roots"] = [str(catalogue_directory / "Inventory.md" / "child")]

        with self.assertRaisesRegex(ConfigError, "catalogue_output may be inside"):
            load_instance_config(config_path=self.config_file(values))

    def test_catalogue_scope_defaults_to_personal(self) -> None:
        values = dict(self.values)
        values.pop("catalogue_scope")

        config = load_instance_config(config_path=self.config_file(values))

        self.assertEqual(config.catalogue_scope, "personal")

    def test_versioned_instance_config_uses_default_instance(self) -> None:
        values = {
            "version": 1,
            "default_instance": "private",
            "instances": {"private": self.values},
        }
        config = load_instance_config(config_path=self.config_file(values))

        self.assertEqual(config.catalogue_scope, "private")

    def test_forbidden_roots_are_additive_and_cannot_be_erased(self) -> None:
        environment = os.environ | {"PROPERTY_INVENTORY_FORBIDDEN_ROOTS": ""}
        config = load_instance_config(config_path=self.config_file(), environ=environment)

        self.assertEqual(config.forbidden_roots, ((self.root / "vault").resolve(),))

        additional = self.root / "code"
        environment["PROPERTY_INVENTORY_FORBIDDEN_ROOTS"] = str(additional)
        config = load_instance_config(
            config_path=self.config_file(),
            forbidden_roots=[self.root / "extra"],
            environ=environment,
        )
        self.assertEqual(
            config.forbidden_roots,
            (
                (self.root / "vault").resolve(),
                additional.resolve(),
                (self.root / "extra").resolve(),
            ),
        )


if __name__ == "__main__":
    unittest.main()
