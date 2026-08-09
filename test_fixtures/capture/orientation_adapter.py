import json
import sys
from pathlib import Path

from PIL import Image, ImageOps

request = json.load(sys.stdin)
source = request["source"]
assert source["coordinate_space"] == "exif_transposed_pixels"
with Image.open(Path(source["image_file"])) as raw:
    oriented = ImageOps.exif_transpose(raw)
    try:
        width, height = oriented.size
    finally:
        if oriented is not raw:
            oriented.close()
assert (width, height) == (source["image_width"], source["image_height"])
print(
    json.dumps(
        {
            "protocol_version": 1,
            "observations": [
                {
                    "type": "ocr",
                    "region": {"x": 0, "y": 0, "width": width, "height": height},
                    "confidence": 1.0,
                    "payload": {"text": "orientation-contract"},
                }
            ],
        }
    )
)
