#!/usr/bin/env python3
"""Regenerate the README GIF and verify its portable story contract."""

from __future__ import annotations

import os
import subprocess
import tempfile
from pathlib import Path

from PIL import Image, ImageChops, ImageStat

ROOT = Path(__file__).resolve().parents[1]
README = ROOT / "README.md"
TAPE = ROOT / "docs" / "assets" / "demo.tape"
GIF = ROOT / "docs" / "assets" / "property-inventory-demo.gif"
OUTPUT = "Output docs/assets/property-inventory-demo.gif"
EXPECTED_SIZE = (1600, 1080)


def validate_gif(path: Path) -> tuple[int, int]:
    if path.stat().st_size > 5_000_000:
        raise SystemExit("README GIF exceeds the five-megabyte public limit")
    with Image.open(path) as image:
        if image.format != "GIF" or image.size != EXPECTED_SIZE or not image.is_animated:
            raise SystemExit("README GIF has the wrong format, dimensions, or animation state")
        frames = image.n_frames
        duration = sum(
            image.seek(index) or image.info.get("duration", 0) for index in range(frames)
        )
    if not 160 <= frames <= 210 or not 6_500 <= duration <= 8_000:
        raise SystemExit("README GIF has an unexpected frame count or duration")
    return frames, duration


def frame_difference(image: Image.Image, first: int, second: int) -> float:
    image.seek(first)
    before = image.convert("RGB")
    image.seek(second)
    after = image.convert("RGB")
    return max(ImageStat.Stat(ImageChops.difference(before, after)).mean)


def validate_story_states(path: Path) -> None:
    """Check the intended blank, command, result, blank sequence.

    Font rasterization differs between macOS and Linux, so cross-platform pixel
    identity is not a valid reproducibility contract. The tape, real CLI output,
    dimensions, timing, and visible state progression are portable.
    """
    with Image.open(path) as image:
        if image.n_frames < 100:
            raise SystemExit("README GIF does not contain the expected story states")
        closing = image.n_frames - 6
        if (
            frame_difference(image, 5, 15) < 0.05
            or frame_difference(image, 15, 25) < 0.02
            or frame_difference(image, 25, 80) < 0.5
            or frame_difference(image, 80, closing) < 0.5
            or frame_difference(image, 5, closing) > 0.02
        ):
            raise SystemExit("README GIF no longer shows the checked state progression")


def main() -> None:
    readme = README.read_text(encoding="utf-8")
    if readme.count('src="docs/assets/property-inventory-demo.gif"') != 1:
        raise SystemExit("README must embed the checked GIF exactly once")
    if 'width="800"' not in readme or "physically checked T25 Torx bit" not in readme:
        raise SystemExit("README GIF must keep its display width and meaningful alt text")

    source = TAPE.read_text(encoding="utf-8")
    required = (
        OUTPUT,
        'Set Width 1600',
        'Set Height 1080',
        'Set TypingSpeed 0ms',
        'Set CursorBlink false',
        'Type "set -e" Enter',
        '"cursor": "#101216"',
        '"cursorAccent": "#101216"',
        'Type "clear" Enter',
        'Type "property-inventory "',
        'Type "search "',
        'Type "\'T25\' "',
        'Type "--summary" Enter',
    )
    if (
        source.count(OUTPUT) != 1
        or any(value not in source for value in required)
        or "\nCopy " in source
        or "\nPaste" in source
    ):
        raise SystemExit("README GIF tape no longer satisfies the release contract")

    committed_shape = validate_gif(GIF)
    validate_story_states(GIF)
    environment = {
        key: value
        for key, value in os.environ.items()
        if not key.startswith("PROPERTY_INVENTORY_")
    }
    with tempfile.TemporaryDirectory(prefix="property-inventory-readme-gif-") as raw:
        temporary = Path(raw)
        regenerated = temporary / "property-inventory-demo.gif"
        temporary_tape = temporary / "demo.tape"
        temporary_tape.write_text(
            source.replace(OUTPUT, f'Output "{regenerated}"'), encoding="utf-8"
        )
        completed = subprocess.run(
            ["vhs", str(temporary_tape)],
            cwd=ROOT,
            env=environment,
            text=True,
            capture_output=True,
            check=False,
        )
        if completed.returncode != 0:
            raise SystemExit(completed.stderr or completed.stdout)
        regenerated_shape = validate_gif(regenerated)
        validate_story_states(regenerated)
        if (
            abs(regenerated_shape[0] - committed_shape[0]) > 4
            or abs(regenerated_shape[1] - committed_shape[1]) > 250
        ):
            raise SystemExit("README GIF timing drifted during regeneration")

    print("README GIF: pass")


if __name__ == "__main__":
    main()
