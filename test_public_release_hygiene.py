from __future__ import annotations

import os
import shutil
import stat
import subprocess
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent
LEAK_CHECK = ROOT / "scripts" / "check-public-leaks.sh"


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

    def test_working_tree_scanner_failure_fails_closed(self) -> None:
        temporary, repository = self._repository()
        self.addCleanup(temporary.cleanup)
        fake_bin = repository / "bin"
        fake_bin.mkdir()
        fake_rg = fake_bin / "rg"
        fake_rg.write_text("#!/bin/sh\nexit 2\n", encoding="utf-8")
        fake_rg.chmod(fake_rg.stat().st_mode | stat.S_IXUSR)
        self._commit(repository)

        completed = subprocess.run(
            ["./check-public-leaks.sh"],
            cwd=repository,
            env={**os.environ, "PATH": f"{fake_bin}:{os.environ['PATH']}"},
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
        fake_bin = repository / "bin"
        fake_bin.mkdir()
        fake_rg = fake_bin / "rg"
        fake_rg.write_text("#!/bin/sh\nexit 1\n", encoding="utf-8")
        fake_rg.chmod(fake_rg.stat().st_mode | stat.S_IXUSR)

        completed = subprocess.run(
            ["./check-public-leaks.sh"],
            cwd=repository,
            env={**os.environ, "PATH": f"{fake_bin}:{os.environ['PATH']}"},
            text=True,
            capture_output=True,
            check=False,
        )

        self.assertEqual(completed.returncode, 0, completed.stderr)


if __name__ == "__main__":
    unittest.main()
