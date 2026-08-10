from __future__ import annotations

import os
import shutil
import stat
import struct
import subprocess
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
LEAK_CHECK = ROOT / "scripts" / "check-public-leaks.sh"
CHECKS_WORKFLOW = ROOT / ".github" / "workflows" / "checks.yml"


class PublicReleaseHygieneTests(unittest.TestCase):
    def _repository(self) -> tuple[tempfile.TemporaryDirectory[str], Path]:
        temporary = tempfile.TemporaryDirectory(prefix="property-inventory-public-audit-")
        repository = Path(temporary.name)
        shutil.copy2(LEAK_CHECK, repository / "check-public-leaks.sh")
        subprocess.run(["git", "init", "-q", repository], check=True)
        return temporary, repository

    @staticmethod
    def _commit(repository: Path) -> None:
        subprocess.run(["git", "-C", repository, "add", "."], check=True)
        subprocess.run(
            [
                "git",
                "-C",
                repository,
                "-c",
                "user.name=Release hygiene test",
                "-c",
                "user.email=test@example.invalid",
                "commit",
                "-qm",
                "fixture",
            ],
            check=True,
        )

    @staticmethod
    def _environment_with_rg(repository: Path, status: int) -> dict[str, str]:
        fake_bin = repository / "bin"
        fake_bin.mkdir()
        fake_rg = fake_bin / "rg"
        fake_rg.write_text(f"#!/bin/sh\nexit {status}\n", encoding="utf-8")
        fake_rg.chmod(fake_rg.stat().st_mode | stat.S_IXUSR)
        return {**os.environ, "PATH": f"{fake_bin}:{os.environ['PATH']}"}

    def test_working_tree_scanner_failure_fails_closed(self) -> None:
        temporary, repository = self._repository()
        self.addCleanup(temporary.cleanup)
        self._commit(repository)

        completed = subprocess.run(
            ["./check-public-leaks.sh"],
            cwd=repository,
            env=self._environment_with_rg(repository, 2),
            text=True,
            capture_output=True,
            check=False,
        )

        self.assertNotEqual(completed.returncode, 0)
        self.assertIn("Cannot scan the public working tree", completed.stderr)

    def test_geojson_fixture_is_an_approved_utf8_text_type(self) -> None:
        temporary, repository = self._repository()
        self.addCleanup(temporary.cleanup)
        fixture = repository / "synthetic-zones.geojson"
        fixture.write_text('{"type":"FeatureCollection","features":[]}\n', encoding="utf-8")
        self._commit(repository)
        completed = subprocess.run(
            ["./check-public-leaks.sh"],
            cwd=repository,
            env=self._environment_with_rg(repository, 1),
            text=True,
            capture_output=True,
            check=False,
        )

        self.assertEqual(completed.returncode, 0, completed.stderr)

    def test_approved_readme_binaries_use_format_validation_not_utf8(self) -> None:
        temporary, repository = self._repository()
        self.addCleanup(temporary.cleanup)
        assets = repository / "docs" / "assets"
        assets.mkdir(parents=True)
        png_header = b"\x89PNG\r\n\x1a\n\x00\x00\x00\rIHDR"
        (assets / "visual.png").write_bytes(png_header + struct.pack(">II", 1440, 720) + b"\x00" * 32)
        (assets / "font.ttf").write_bytes(b"\x00\x01\x00\x00" + b"\x00" * 64)
        self._commit(repository)
        completed = subprocess.run(
            ["./check-public-leaks.sh"],
            cwd=repository,
            env=self._environment_with_rg(repository, 1),
            text=True,
            capture_output=True,
            check=False,
        )

        self.assertEqual(completed.returncode, 0, completed.stderr)

    def test_readme_png_with_unapproved_dimensions_fails_closed(self) -> None:
        temporary, repository = self._repository()
        self.addCleanup(temporary.cleanup)
        assets = repository / "docs" / "assets"
        assets.mkdir(parents=True)
        png_header = b"\x89PNG\r\n\x1a\n\x00\x00\x00\rIHDR"
        (assets / "visual.png").write_bytes(png_header + struct.pack(">II", 100, 100) + b"\x00" * 32)
        self._commit(repository)
        completed = subprocess.run(
            ["./check-public-leaks.sh"],
            cwd=repository,
            env=self._environment_with_rg(repository, 1),
            text=True,
            capture_output=True,
            check=False,
        )

        self.assertNotEqual(completed.returncode, 0)
        self.assertIn("unexpected format, size, or dimensions", completed.stderr)

    def test_readme_cli_example_is_checked_by_the_linux_audit_job(self) -> None:
        workflow = CHECKS_WORKFLOW.read_text(encoding="utf-8")
        _, audit_job = workflow.split("\n  audit:\n", maxsplit=1)

        self.assertIn("runs-on: ubuntu-latest", audit_job)
        self.assertEqual(audit_job.count("run: python scripts/check-readme-example.py"), 1)


if __name__ == "__main__":
    unittest.main()
