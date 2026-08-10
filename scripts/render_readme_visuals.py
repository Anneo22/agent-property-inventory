#!/usr/bin/env python3
"""Render the README's object-led visuals from a real synthetic CLI record.

THESIS: physical evidence becomes agent memory, not software chrome.
OWN-WORLD: warm accession paper, cobalt routes, orange marks, black objects.
STORY: show one object, ask one question, then see the connected physical world.
FIRST VIEWPORT: a full-bleed specimen photograph with at most four large labels.
FORM: evidence accession, assigned grounded direction, seed 17bc3095.
FINISH: unreviewed and undocumented is unfinished; this build ends with the finish review, the verdict, and DESIGN.md.
"""

import argparse
import json
import os
import subprocess
import sys
import tempfile
from pathlib import Path

from PIL import Image, ImageDraw, ImageEnhance, ImageFont, ImageOps

ROOT = Path(__file__).resolve().parents[1]
ENTRYPOINT = ROOT / "property_inventory.py"
ASSETS = ROOT / "docs" / "assets"
SOURCE_PHOTO = ASSETS / "workshop-specimen.png"
FONT_PATH = ASSETS / "fonts" / "ArchivoBlack-Regular.ttf"
SIZE = (1440, 720)
INK = "#111719"
BLUE = "#274BE7"
ORANGE = "#FF5C35"
GREEN = "#C9EF73"
WHITE = "#FFFDF7"


def font(size: int) -> ImageFont.FreeTypeFont:
    return ImageFont.truetype(FONT_PATH, size=size)


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


def demo_results() -> tuple[dict, dict, dict, dict]:
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
        known = run_cli(environment, "search", "hex bit", "--summary")
        t25 = run_cli(environment, "search", "T25", "--summary")
        unknown = run_cli(environment, "search", "not recorded", "--summary")
        integrity = run_cli(environment, "status", "--summary")
    return known, t25, unknown, integrity


def validate_results(known: dict, t25: dict, unknown: dict, integrity: dict) -> tuple[dict, dict]:
    if not known.get("matching_record_found") or known.get("count") != 1:
        raise SystemExit("known-item demo did not return one recorded match")
    match = known.get("matches", [{}])[0]
    expected = {
        "ownership": "confirmed",
        "condition": "working",
        "location": "Tool drawer",
        "last_physical_check_on": "2026-08-09",
        "evidence_types": ["physical_check"],
    }
    if any(match.get(key) != value for key, value in expected.items()):
        raise SystemExit("known-item demo did not return the checked physical facts")
    if not t25.get("matching_record_found") or t25.get("count") != 1:
        raise SystemExit("T25 demo did not return one recorded match")
    t25_match = t25.get("matches", [{}])[0]
    if t25_match.get("name") != "T25 Torx bit" or any(
        t25_match.get(key) != value for key, value in expected.items()
    ):
        raise SystemExit("T25 demo did not return the checked physical facts")
    if unknown.get("meaning") != "unknown, not absent" or unknown.get("count") != 0:
        raise SystemExit("no-match demo did not preserve unknown")
    if integrity != {
        "integrity_gate": "pass",
        "verification_failures": [],
        "foreign_key_failures": 0,
    }:
        raise SystemExit("integrity demo did not return an exact clean result")
    return match, t25_match


def paper_color(photo: Image.Image) -> tuple[int, int, int]:
    return photo.convert("RGB").getpixel((12, 12))


def base(photo: Image.Image) -> Image.Image:
    return Image.new("RGB", SIZE, paper_color(photo))


def cover_photo(photo: Image.Image) -> Image.Image:
    fitted = ImageOps.fit(photo.convert("RGB"), SIZE, Image.Resampling.LANCZOS)
    return ImageEnhance.Sharpness(fitted).enhance(1.15)


def label(draw: ImageDraw.ImageDraw, xy: tuple[int, int], text: str, color: str, size: int) -> None:
    draw.text(xy, text, font=font(size), fill=color)


def route(draw: ImageDraw.ImageDraw, points: list[tuple[int, int]], color: str = BLUE) -> None:
    draw.line(points, fill=color, width=8, joint="curve")
    x, y = points[0]
    draw.ellipse((x - 10, y - 10, x + 10, y + 10), fill=color)


def pill(
    draw: ImageDraw.ImageDraw,
    xy: tuple[int, int],
    text: str,
    fill: str,
    foreground: str = INK,
) -> tuple[int, int, int, int]:
    typeface = font(25)
    box = draw.textbbox((0, 0), text, font=typeface)
    width = box[2] - box[0] + 44
    rectangle = (xy[0], xy[1], xy[0] + width, xy[1] + 54)
    draw.rounded_rectangle(rectangle, radius=27, fill=fill)
    draw.text((xy[0] + 22, xy[1] + 11), text, font=typeface, fill=foreground)
    return rectangle


def frame_show(photo: Image.Image) -> Image.Image:
    image = cover_photo(photo)
    draw = ImageDraw.Draw(image)
    label(draw, (66, 54), "CHECK IT.", INK, 88)
    draw.rectangle((66, 166, 340, 178), fill=ORANGE)
    check_box = pill(draw, (66, 198), "IN PERSON", BLUE, WHITE)
    route(draw, [(894, 258), (620, 258), (620, 225), (check_box[2], 225)])
    draw.rounded_rectangle((894, 82, 1368, 446), radius=42, outline=BLUE, width=10)
    return image


def frame_know(photo: Image.Image, match: dict) -> Image.Image:
    original = cover_photo(photo)
    paper = Image.new("RGB", SIZE, paper_color(photo))
    image = Image.blend(original, paper, 0.38)
    draw = ImageDraw.Draw(image)
    label(draw, (60, 48), "KNOW IT.", INK, 80)
    owned_box = pill(draw, (60, 174), "OWNED", GREEN)
    location_box = pill(draw, (60, 254), match["location"].upper(), BLUE, WHITE)
    condition_box = pill(draw, (60, 334), match["condition"].upper(), ORANGE, WHITE)
    draw.line((owned_box[2], 201, 512, 201, 512, 361), fill=BLUE, width=8, joint="curve")
    draw.line((location_box[2], 281, 512, 281), fill=BLUE, width=8)
    draw.line((condition_box[2], 361, 512, 361), fill=BLUE, width=8)
    route(draw, [(904, 258), (512, 258)])
    draw.rounded_rectangle((894, 82, 1368, 446), radius=42, outline=BLUE, width=10)
    return image


def frame_use(photo: Image.Image) -> Image.Image:
    image = cover_photo(photo)
    draw = ImageDraw.Draw(image)
    label(draw, (66, 50), "DECIDE.", INK, 88)
    question_box = pill(draw, (66, 164), "BUY ANOTHER?", ORANGE, WHITE)
    label(draw, (66, 236), "ALREADY\nOWNED.", BLUE, 65)
    route(draw, [(900, 258), (720, 258), (620, 191), (question_box[2], 191)], ORANGE)
    draw.rounded_rectangle((894, 82, 1368, 446), radius=42, outline=ORANGE, width=10)
    return image


def render_a(photo: Image.Image, match: dict, output: Path) -> None:
    frames = [frame_show(photo), frame_know(photo, match), frame_use(photo)]
    indexed = [
        frame.quantize(colors=256, method=Image.Quantize.MEDIANCUT, dither=Image.Dither.FLOYDSTEINBERG)
        for frame in frames
    ]
    indexed[0].save(
        output,
        save_all=True,
        append_images=indexed[1:],
        duration=[2800, 2800, 3200],
        loop=0,
        disposal=2,
        optimize=True,
    )


def render_b(photo: Image.Image, match: dict, output: Path) -> None:
    image = base(photo)
    draw = ImageDraw.Draw(image)
    draw.rectangle((0, 0, 620, 720), fill=BLUE)
    label(draw, (66, 62), "ASK BEFORE\nYOU ACT.", WHITE, 72)
    label(draw, (66, 300), "DO I OWN\nA T25?", GREEN, 58)
    crop = photo.crop((700, 70, 1536, 650)).convert("RGB")
    crop = ImageOps.fit(crop, (720, 500), Image.Resampling.LANCZOS)
    crop = ImageEnhance.Sharpness(crop).enhance(1.15)
    image.paste(crop, (690, 70))
    draw = ImageDraw.Draw(image)
    draw.ellipse((1190, 354, 1248, 412), outline=ORANGE, width=9)
    route(draw, [(1246, 382), (1302, 278)], ORANGE)
    pill(draw, (1280, 216), "T25", ORANGE, WHITE)
    pill(draw, (744, 594), "YES", GREEN)
    label(draw, (880, 600), f"IN {match['location'].upper()}", INK, 34)
    pill(draw, (880, 650), "CHECKED IN PERSON", BLUE, WHITE)
    image.save(output, optimize=True)


def room(draw: ImageDraw.ImageDraw, box: tuple[int, int, int, int], name: str) -> None:
    draw.rectangle(box, outline=INK, width=7)
    label(draw, (box[0] + 24, box[1] + 20), name, INK, 25)


def render_c(photo: Image.Image, output: Path) -> None:
    image = base(photo)
    draw = ImageDraw.Draw(image)
    label(draw, (60, 44), "YOUR PHYSICAL WORLD.\nNOW QUERYABLE.", INK, 60)
    label(draw, (64, 177), "ITEMS IN PLACES", BLUE, 22)
    room(draw, (64, 212, 400, 450), "TOOL DRAWER")
    room(draw, (400, 212, 750, 450), "TRAVEL BAG")
    room(draw, (64, 450, 480, 665), "BIKE BOX")
    room(draw, (480, 450, 750, 665), "MUG CUPBOARD")
    # A tool, suitcase, bicycle, and mug keep the map grounded in physical objects.
    draw.line((278, 360, 320, 318), fill=INK, width=11)
    draw.ellipse((268, 350, 288, 370), outline=INK, width=7)
    draw.ellipse((310, 308, 330, 328), outline=INK, width=7)
    draw.rounded_rectangle((552, 310, 608, 352), radius=7, fill=INK)
    draw.arc((566, 294, 594, 318), start=180, end=360, fill=INK, width=7)
    draw.ellipse((274, 522, 306, 554), outline=INK, width=7)
    draw.ellipse((316, 522, 348, 554), outline=INK, width=7)
    draw.line((290, 538, 310, 510, 332, 538, 290, 538, 316, 522), fill=INK, width=6)
    draw.rectangle((586, 568, 624, 610), outline=INK, width=7)
    draw.arc((610, 574, 642, 604), start=270, end=90, fill=INK, width=7)

    draw.line((750, 450, 800, 450), fill=BLUE, width=8)
    draw.polygon(((800, 450), (782, 438), (782, 462)), fill=BLUE)
    records_box = pill(draw, (800, 423), "RECORDS", GREEN)
    draw.line((records_box[2], 450, 990, 450), fill=INK, width=8)
    draw.ellipse((990, 340, 1210, 560), fill=BLUE)
    draw.polygon(((1010, 450), (988, 436), (988, 464)), fill=INK)
    draw.ellipse((1040, 390, 1160, 510), fill=GREEN)
    label(draw, (1034, 428), "AGENT", INK, 27)
    draw.line((1170, 360, 1215, 360), fill=INK, width=7)
    draw.line((1170, 450, 1215, 450), fill=INK, width=7)
    draw.line((1170, 540, 1215, 540), fill=INK, width=7)
    draw.polygon(((1215, 360), (1197, 348), (1197, 372)), fill=INK)
    draw.polygon(((1215, 450), (1197, 438), (1197, 462)), fill=INK)
    draw.polygon(((1215, 540), (1197, 528), (1197, 552)), fill=INK)
    pill(draw, (1215, 333), "PACK", GREEN)
    pill(draw, (1215, 423), "REPAIR", ORANGE, WHITE)
    pill(draw, (1215, 513), "INSURANCE", BLUE, WHITE)
    image.save(output, optimize=True)


def render(output_dir: Path) -> None:
    photo = Image.open(SOURCE_PHOTO)
    match, t25_match = validate_results(*demo_results())
    output_dir.mkdir(parents=True, exist_ok=True)
    render_a(photo, match, output_dir / "physical-memory.gif")
    render_b(photo, t25_match, output_dir / "ask-before-acting.png")
    render_c(photo, output_dir / "physical-world-map.png")


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--output-dir", type=Path, default=ASSETS)
    args = parser.parse_args()
    render(args.output_dir.resolve())


if __name__ == "__main__":
    main()
