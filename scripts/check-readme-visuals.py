#!/usr/bin/env python3
"""Fail when README visuals or their reproducible sources drift."""

import xml.etree.ElementTree as ET
from pathlib import Path

from PIL import Image

ROOT = Path(__file__).resolve().parents[1]
README = ROOT / "README.md"
ASSETS = ROOT / "docs" / "assets"
EXPECTED_LINKS = {
    "docs/assets/property-inventory-demo.gif",
    "docs/assets/evidence-model.svg",
    "docs/assets/trusted-path.svg",
}


def check_svg(path: Path, expected_size: tuple[int, int]) -> None:
    root = ET.parse(path).getroot()
    actual_size = (int(root.attrib["width"]), int(root.attrib["height"]))
    if actual_size != expected_size:
        raise SystemExit(f"{path.name}: expected {expected_size}, got {actual_size}")
    if root.attrib.get("role") != "img":
        raise SystemExit(f"{path.name}: missing role=img")
    namespace = {"svg": "http://www.w3.org/2000/svg"}
    if root.find("svg:title", namespace) is None or root.find("svg:desc", namespace) is None:
        raise SystemExit(f"{path.name}: missing accessible title or description")
    if root.findall(".//svg:image", namespace):
        raise SystemExit(f"{path.name}: embedded images are not allowed")


def main() -> None:
    readme = README.read_text(encoding="utf-8")
    if "One bike-parts session" in readme:
        raise SystemExit("README still contains the retired bike-parts anecdote")
    for link in EXPECTED_LINKS:
        if readme.count(f"]({link})") != 1:
            raise SystemExit(f"README must reference {link} exactly once")

    tape = (ASSETS / "demo.tape").read_text(encoding="utf-8")
    if tape.count("Output docs/assets/property-inventory-demo.gif") != 1:
        raise SystemExit("demo.tape must declare the README GIF output exactly once")

    with Image.open(ASSETS / "property-inventory-demo.gif") as demo:
        if demo.size != (960, 560):
            raise SystemExit(f"demo GIF: expected 960x560, got {demo.size}")
        if getattr(demo, "n_frames", 1) < 100:
            raise SystemExit("demo GIF has too few frames to demonstrate the CLI flow")

    check_svg(ASSETS / "trusted-path.svg", (960, 600))
    check_svg(ASSETS / "evidence-model.svg", (960, 650))
    print("README visuals: pass")


if __name__ == "__main__":
    main()
