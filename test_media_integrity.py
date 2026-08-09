"""Focused byte-level checks for content-addressed inventory media."""

# ruff: noqa: E402

from __future__ import annotations

import hashlib
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE / "src"))

from property_inventory.verify import media_asset_failures


class MediaIntegrityTest(unittest.TestCase):
    def test_absent_root_is_accepted_when_there_are_no_assets(self) -> None:
        self.assertEqual(media_asset_failures([], Path("/path/that/does/not/exist")), [])
        self.assertEqual(media_asset_failures([], None), [])

    def test_assets_require_an_explicit_media_root(self) -> None:
        asset = {"asset_id": "asset-required", "sha256": "a" * 64, "byte_size": 1}
        self.assertEqual(
            media_asset_failures([asset], None),
            ["media assets exist but --media-root was not supplied"],
        )

    def test_checks_content_addressed_path_size_and_hash(self) -> None:
        payload = b"physical evidence bytes"
        digest = hashlib.sha256(payload).hexdigest()
        asset = {"asset_id": "asset-photo", "sha256": digest, "byte_size": len(payload)}

        with tempfile.TemporaryDirectory(prefix="inventory-media-") as temporary:
            root = Path(temporary)
            path = root / "sha256" / digest[:2] / digest
            path.parent.mkdir(parents=True)
            path.write_bytes(payload)
            self.assertEqual(media_asset_failures([asset], root), [])

            path.write_bytes(b"tampered")
            failures = media_asset_failures([asset], root)

            path.write_bytes(b"x" * len(payload))
            hash_failures = media_asset_failures([asset], root)

        self.assertEqual(len(failures), 1)
        self.assertIn("asset-photo", failures[0])
        self.assertIn(str(path), failures[0])
        self.assertIn("byte_size mismatch", failures[0])
        self.assertEqual(len(hash_failures), 1)
        self.assertIn("asset-photo", hash_failures[0])
        self.assertIn(str(path), hash_failures[0])
        self.assertIn("sha256 mismatch", hash_failures[0])

    def test_missing_asset_names_asset_and_expected_path(self) -> None:
        digest = "f" * 64
        asset = {"asset_id": "asset-missing", "sha256": digest, "byte_size": 1}
        root = Path("/definitely-not-an-inventory-media-root")

        failures = media_asset_failures([asset], root)

        self.assertEqual(len(failures), 1)
        self.assertIn("asset-missing", failures[0])
        self.assertIn(str(root / "sha256" / "ff" / digest), failures[0])

    def test_rejects_symlinked_managed_components_without_opening_external_bytes(self) -> None:
        payload = b"external sentinel bytes"
        digest = hashlib.sha256(payload).hexdigest()
        asset = {"asset_id": "asset-symlink", "sha256": digest, "byte_size": len(payload)}

        with tempfile.TemporaryDirectory(prefix="inventory-media-") as temporary:
            temporary_path = Path(temporary)
            external_root = temporary_path / "external"
            external_path = external_root / "sha256" / digest[:2] / digest
            external_path.parent.mkdir(parents=True)
            external_path.write_bytes(payload)

            for component, expected_description in (
                ("media root", "media root"),
                ("sha256", "sha256 directory"),
                ("prefix", "sha256 prefix directory"),
                ("leaf", "media asset"),
            ):
                with self.subTest(component=component):
                    managed_root = temporary_path / f"managed-{component}"
                    if component == "media root":
                        managed_root.symlink_to(external_root, target_is_directory=True)
                    elif component == "sha256":
                        managed_root.mkdir()
                        (managed_root / "sha256").symlink_to(
                            external_root / "sha256", target_is_directory=True
                        )
                    elif component == "prefix":
                        (managed_root / "sha256").mkdir(parents=True)
                        (managed_root / "sha256" / digest[:2]).symlink_to(
                            external_path.parent, target_is_directory=True
                        )
                    else:
                        managed_path = managed_root / "sha256" / digest[:2]
                        managed_path.mkdir(parents=True)
                        (managed_path / digest).symlink_to(external_path)

                    with patch.object(
                        Path,
                        "open",
                        side_effect=AssertionError("external media bytes must not be opened"),
                    ) as open_mock:
                        failures = media_asset_failures([asset], managed_root)

                    open_mock.assert_not_called()
                    self.assertEqual(len(failures), 1)
                    self.assertIn(expected_description, failures[0])
                    self.assertIn("unsafe symlinked", failures[0])
                    self.assertEqual(external_path.read_bytes(), payload)

    def test_allows_a_symlinked_ancestor_outside_the_managed_media_root(self) -> None:
        payload = b"managed media through a system-style alias"
        digest = hashlib.sha256(payload).hexdigest()
        asset = {"asset_id": "asset-alias", "sha256": digest, "byte_size": len(payload)}

        with tempfile.TemporaryDirectory(prefix="inventory-media-") as temporary:
            temporary_path = Path(temporary)
            real_parent = temporary_path / "real-parent"
            managed_root = real_parent / "media"
            path = managed_root / "sha256" / digest[:2] / digest
            path.parent.mkdir(parents=True)
            path.write_bytes(payload)
            system_style_alias = temporary_path / "system-style-alias"
            system_style_alias.symlink_to(real_parent, target_is_directory=True)

            self.assertEqual(media_asset_failures([asset], system_style_alias / "media"), [])


if __name__ == "__main__":
    unittest.main()
