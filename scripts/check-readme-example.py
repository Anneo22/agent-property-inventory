#!/usr/bin/env python3
"""Verify that the README's terminal example comes from the real CLI."""

import difflib
import json
import os
import subprocess
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
ENTRYPOINT = ROOT / "property_inventory.py"
README = ROOT / "README.md"
START = "<!-- readme-example:start -->"
END = "<!-- readme-example:end -->"
CAPTURE_START = "<!-- readme-capture:start -->"
CAPTURE_END = "<!-- readme-capture:end -->"


def clean_environment(base: Path) -> dict[str, str]:
    environment = {
        key: value
        for key, value in os.environ.items()
        if not key.startswith("PROPERTY_INVENTORY_")
    }
    environment.update(
        {
            "PROPERTY_INVENTORY_ROOT": str(base / "inventory"),
            "PROPERTY_INVENTORY_RUNTIME": str(base / "runtime"),
            "PROPERTY_INVENTORY_MEDIA_ROOT": str(base / "media"),
            "PROPERTY_INVENTORY_CATALOGUE_OUTPUT": str(base / "notes" / "Inventory.md"),
        }
    )
    return environment


def run_cli(environment: dict[str, str], *arguments: str) -> dict:
    completed = subprocess.run(
        [sys.executable, str(ENTRYPOINT), *arguments],
        cwd=ROOT,
        env=environment,
        text=True,
        capture_output=True,
        check=False,
    )
    if completed.returncode != 0:
        raise SystemExit(completed.stderr or completed.stdout)
    return json.loads(completed.stdout)


def demo_result() -> dict:
    with tempfile.TemporaryDirectory(prefix="property-inventory-readme-") as temporary:
        environment = clean_environment(Path(temporary))
        run_cli(environment, "init")
        run_cli(
            environment,
            "add-location",
            "--location-id",
            "loc-tool-drawer",
            "--name",
            "Tool drawer",
            "--kind",
            "container",
        )
        run_cli(
            environment,
            "discover",
            "--actor",
            "Demo",
            "--source-ref",
            "Checked in person",
            "--name",
            "T25 Torx bit",
            "--category",
            "tool",
            "--checked-on",
            "2026-08-09",
            "--location-id",
            "loc-tool-drawer",
            "--new-model",
            "--new-unit",
            "--brand",
            "Wera",
            "--model",
            "T25",
            "--quantity",
            "1",
            "--unit",
            "piece",
            "--condition",
            "working",
        )
        result = run_cli(environment, "search", "T25", "--summary")
        unknown = run_cli(environment, "search", "not recorded", "--summary")
        integrity = run_cli(environment, "status", "--summary")

    if unknown.get("meaning") != "unknown, not absent" or unknown.get("count") != 0:
        raise SystemExit("README fixture no longer preserves unknown on an empty search")
    if integrity != {
        "integrity_gate": "pass",
        "verification_failures": [],
        "foreign_key_failures": 0,
    }:
        raise SystemExit("README fixture no longer passes the integrity gate")
    return result


def expected_block() -> str:
    return (
        "```console\n"
        '$ property-inventory search "T25" --summary\n'
        f"{json.dumps(demo_result(), indent=2)}\n"
        "```"
    )


def expected_capture_block() -> str:
    return """```bash
property-inventory add-location \\
  --location-id loc-tool-drawer \\
  --name "Tool drawer" \\
  --kind container

property-inventory discover \\
  --actor "Owner" \\
  --source-ref "Checked in person" \\
  --name "T25 Torx bit" \\
  --category tool \\
  --checked-on "$(date +%F)" \\
  --location-id loc-tool-drawer \\
  --new-model \\
  --new-unit \\
  --brand Wera \\
  --model T25 \\
  --quantity 1 \\
  --unit piece \\
  --condition working

property-inventory search "T25" --summary
```"""


def main() -> None:
    readme = README.read_text(encoding="utf-8")
    if readme.count(START) != 1 or readme.count(END) != 1:
        raise SystemExit("README must contain one checked terminal-example block")
    actual = readme.split(START, maxsplit=1)[1].split(END, maxsplit=1)[0].strip()
    expected = expected_block()
    if actual != expected:
        difference = "\n".join(
            difflib.unified_diff(
                actual.splitlines(),
                expected.splitlines(),
                fromfile="README.md",
                tofile="real CLI output",
                lineterm="",
            )
        )
        raise SystemExit(f"README terminal example has drifted:\n{difference}")
    if readme.count(CAPTURE_START) != 1 or readme.count(CAPTURE_END) != 1:
        raise SystemExit("README must contain one checked first-capture block")
    actual_capture = (
        readme.split(CAPTURE_START, maxsplit=1)[1]
        .split(CAPTURE_END, maxsplit=1)[0]
        .strip()
    )
    expected_capture = expected_capture_block()
    if actual_capture != expected_capture:
        difference = "\n".join(
            difflib.unified_diff(
                actual_capture.splitlines(),
                expected_capture.splitlines(),
                fromfile="README.md",
                tofile="checked first-capture commands",
                lineterm="",
            )
        )
        raise SystemExit(f"README first-capture example has drifted:\n{difference}")
    without_checked_gif = readme.replace(
        "docs/assets/property-inventory-demo.gif", ""
    )
    if "docs/assets/" in without_checked_gif:
        raise SystemExit("README still references retired decorative assets")
    print("README CLI example: pass")


if __name__ == "__main__":
    main()
