import hashlib
import json
import sys
from pathlib import Path

request = json.load(sys.stdin)
assert request["protocol_version"] == 1
image_path = Path(request["source"]["image_file"])
image_bytes = image_path.read_bytes()
assert image_path.name == "overview-image"
assert image_path.parent == Path(".")
assert len(image_bytes) == request["source"]["byte_length"]
assert hashlib.sha256(image_bytes).hexdigest() == request["source"]["sha256"]
assert request["source"]["coordinate_space"] == "exif_transposed_pixels"
print(
    json.dumps(
        {
            "protocol_version": 1,
            "predicted_segments": [
                {
                    "segment_id": "detected-object",
                    "region": {"x": 1, "y": 2, "width": 6, "height": 4},
                }
            ],
            "observations": [
                {
                    "type": "ocr",
                    "region": {"x": 1, "y": 2, "width": 3, "height": 4},
                    "confidence": 0.8,
                    "payload": {"text": "Model AB-1", "serial": "unknown"},
                },
                {
                    "type": "barcode",
                    "region": {"x": 5, "y": 2, "width": 2, "height": 2},
                    "confidence": 0.9,
                    "payload": {"value": "123456789", "format": "EAN-13"},
                },
            ],
        }
    )
)
