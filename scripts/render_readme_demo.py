#!/usr/bin/env python3
"""Build the README GIF from real CLI results with deterministic pixels."""

import argparse
import json
import os
import subprocess
import sys
import tempfile
from pathlib import Path

from PIL import Image, ImageDraw, ImageFont

ROOT = Path(__file__).resolve().parents[1]
ENTRYPOINT = ROOT / "property_inventory.py"
LOW_RESOLUTION = (480, 280)
OUTPUT_SIZE = (960, 560)
FRAME_DURATION_MS = 3500


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


def demo_results() -> tuple[dict, dict, dict]:
    with tempfile.TemporaryDirectory(prefix="property-inventory-readme-") as temporary:
        base = Path(temporary)
        environment = {
            **{
                key: value
                for key, value in os.environ.items()
                if not key.startswith("PROPERTY_INVENTORY_")
            },
            "PROPERTY_INVENTORY_ROOT": str(base / "inventory"),
            "PROPERTY_INVENTORY_RUNTIME": str(base / "runtime"),
            "PROPERTY_INVENTORY_MEDIA_ROOT": str(base / "media"),
            "PROPERTY_INVENTORY_CATALOGUE_OUTPUT": str(base / "notes" / "Inventory.md"),
        }
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
            "1/4 inch hex bit set",
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
            "Bit-Check 30",
            "--quantity",
            "1",
            "--unit",
            "set",
            "--condition",
            "working",
        )
        known = run_cli(environment, "search", "hex bit", "--summary")
        unknown = run_cli(
            environment,
            "search",
            "bicycle tyre repair kit",
            "--summary",
        )
        integrity = run_cli(environment, "status", "--summary")
    return known, unknown, integrity


def validate_results(known: dict, unknown: dict, integrity: dict) -> None:
    if not known.get("matching_record_found") or known.get("count") != 1:
        raise SystemExit("known-item demo did not return exactly one recorded match")
    match = known.get("matches", [{}])[0]
    if (
        match.get("ownership") != "confirmed"
        or match.get("condition") != "working"
        or match.get("location") != "Tool drawer"
        or match.get("last_physical_check_on") != "2026-08-09"
        or match.get("evidence_types") != ["physical_check"]
    ):
        raise SystemExit("known-item demo did not return the checked physical facts")
    if (
        unknown.get("meaning") != "unknown, not absent"
        or unknown.get("matching_record_found") is not False
        or unknown.get("count") != 0
        or unknown.get("matches") != []
    ):
        raise SystemExit("no-match demo did not preserve unknown")
    if integrity != {
        "integrity_gate": "pass",
        "verification_failures": [],
        "foreign_key_failures": 0,
    }:
        raise SystemExit("integrity demo did not return an exact clean result")


def lines_for_results(known: dict, unknown: dict, integrity: dict) -> list[list[str]]:
    validate_results(known, unknown, integrity)
    match = known["matches"][0]
    return [
        [
            "1 / 3  KNOWN: CHECKED FACTS + EVIDENCE TYPE",
            "",
            "$ property-inventory search 'hex bit' --summary",
            "{",
            f'  "matching_record_found": {str(known["matching_record_found"]).lower()},',
            f'  "name": "{match["name"]}",',
            f'  "ownership": "{match["ownership"]}",',
            f'  "condition": "{match["condition"]}",',
            f'  "location": "{match["location"]}",',
            f'  "last_physical_check_on": "{match["last_physical_check_on"]}",',
            f'  "evidence_types": {json.dumps(match["evidence_types"])},',
            f'  "page_count": {known["page_count"]},',
            f'  "truncated": {str(known["truncated"]).lower()}',
            "}",
        ],
        [
            "2 / 3  NO MATCH: PRESERVE UNKNOWN",
            "",
            "$ property-inventory search \\",
            "    'bicycle tyre repair kit' --summary",
            "{",
            f'  "meaning": "{unknown["meaning"]}",',
            f'  "matching_record_found": {str(unknown["matching_record_found"]).lower()},',
            f'  "count": {unknown["count"]},',
            '  "matches": [],',
            f'  "page_count": {unknown["page_count"]},',
            f'  "truncated": {str(unknown["truncated"]).lower()}',
            "}",
        ],
        [
            "3 / 3  INTEGRITY: VERIFY BEFORE USE",
            "",
            "$ property-inventory status --summary",
            "{",
            f'  "integrity_gate": "{integrity["integrity_gate"]}",',
            f'  "verification_failures": {json.dumps(integrity["verification_failures"])},',
            f'  "foreign_key_failures": {integrity["foreign_key_failures"]}',
            "}",
            "",
            "The local record passed every integrity check.",
        ],
    ]


def render_frame(lines: list[str]) -> Image.Image:
    image = Image.new("RGB", LOW_RESOLUTION, "#0d1117")
    draw = ImageDraw.Draw(image)
    font = ImageFont.load_default()
    draw.rounded_rectangle((8, 8, 471, 271), radius=7, fill="#161b22", outline="#30363d")
    for index, color in enumerate(("#ff5f57", "#febc2e", "#28c840")):
        x = 18 + index * 10
        draw.ellipse((x, 17, x + 6, 23), fill=color)
    for index, line in enumerate(lines):
        color = "#58a6ff" if index == 0 else "#f0f6fc"
        if line.startswith("$") or line.startswith("    '"):
            color = "#7ee787"
        draw.text((18, 34 + index * 14), line, font=font, fill=color)
    return image.resize(OUTPUT_SIZE, Image.Resampling.NEAREST)


def render(output: Path) -> None:
    frames = [render_frame(lines) for lines in lines_for_results(*demo_results())]
    indexed = [
        frame.quantize(colors=256, method=Image.Quantize.MEDIANCUT, dither=Image.Dither.NONE)
        for frame in frames
    ]
    output.parent.mkdir(parents=True, exist_ok=True)
    indexed[0].save(
        output,
        save_all=True,
        append_images=indexed[1:],
        duration=[FRAME_DURATION_MS] * len(indexed),
        loop=0,
        disposal=2,
        optimize=False,
    )


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--output",
        type=Path,
        default=ROOT / "docs" / "assets" / "property-inventory-demo.gif",
    )
    args = parser.parse_args()
    render(args.output.resolve())


if __name__ == "__main__":
    main()
