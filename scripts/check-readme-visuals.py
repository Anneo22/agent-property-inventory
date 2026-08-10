#!/usr/bin/env python3
"""Fail when README visuals or their reproducible sources drift."""

import subprocess
import sys
import tempfile
from pathlib import Path

from PIL import Image

ROOT = Path(__file__).resolve().parents[1]
README = ROOT / "README.md"
ASSETS = ROOT / "docs" / "assets"
RENDERER = ROOT / "scripts" / "render_readme_visuals.py"
EXPECTED_LINKS = {
    "docs/assets/physical-memory.gif",
    "docs/assets/ask-before-acting.png",
    "docs/assets/physical-world-map.png",
}
EXPECTED_VISUALS = {
    "physical-memory.gif": (3, [2800, 2800, 3200]),
    "ask-before-acting.png": (1, [None]),
    "physical-world-map.png": (1, [None]),
}
RETIRED_LINKS = {
    "docs/assets/property-inventory-demo.gif",
    "docs/assets/physical-world.svg",
}


def frame_durations(image: Image.Image) -> list[int | None]:
    durations = []
    for frame in range(getattr(image, "n_frames", 1)):
        image.seek(frame)
        durations.append(image.info.get("duration"))
    return durations


def check_same_pixels(expected_path: Path, actual_path: Path) -> None:
    with Image.open(expected_path) as expected, Image.open(actual_path) as actual:
        if expected.size != actual.size or expected.n_frames != actual.n_frames:
            raise SystemExit(f"{expected_path.name}: regenerated dimensions or frames differ")
        if frame_durations(expected) != frame_durations(actual):
            raise SystemExit(f"{expected_path.name}: regenerated timing differs")
        for frame in range(expected.n_frames):
            expected.seek(frame)
            actual.seek(frame)
            if expected.convert("RGBA").tobytes() != actual.convert("RGBA").tobytes():
                raise SystemExit(f"{expected_path.name}: regenerated pixels differ")


def check_reproduction() -> None:
    with tempfile.TemporaryDirectory(prefix="property-inventory-visual-check-") as temporary:
        subprocess.run(
            [sys.executable, str(RENDERER), "--output-dir", temporary],
            cwd=ROOT,
            check=True,
        )
        for name in EXPECTED_VISUALS:
            check_same_pixels(ASSETS / name, Path(temporary) / name)


def main() -> None:
    readme = README.read_text(encoding="utf-8")
    if "One bike-parts session" in readme:
        raise SystemExit("README still contains the retired bike-parts anecdote")
    for link in EXPECTED_LINKS:
        if readme.count(f"]({link})") != 1:
            raise SystemExit(f"README must reference {link} exactly once")
    for link in RETIRED_LINKS:
        if link in readme:
            raise SystemExit(f"README still references retired visual {link}")

    check_reproduction()
    for name, (expected_frames, expected_durations) in EXPECTED_VISUALS.items():
        with Image.open(ASSETS / name) as visual:
            if visual.size != (1440, 720):
                raise SystemExit(f"{name}: expected 1440x720, got {visual.size}")
            if getattr(visual, "n_frames", 1) != expected_frames:
                raise SystemExit(f"{name}: unexpected frame count")
            if frame_durations(visual) != expected_durations:
                raise SystemExit(f"{name}: unexpected frame timing")

    with Image.open(ASSETS / "workshop-specimen.png") as source:
        if source.size != (1536, 1024):
            raise SystemExit("workshop specimen source has unexpected dimensions")
    if not (ASSETS / "fonts" / "ArchivoBlack-Regular.ttf").is_file():
        raise SystemExit("README visual font is missing")
    print("README visuals: pass")


if __name__ == "__main__":
    main()
