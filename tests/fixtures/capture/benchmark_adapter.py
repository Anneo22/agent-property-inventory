import hashlib
import json
import sys
from pathlib import Path

request = json.load(sys.stdin)
source = request["source"]
image = Path(source["image_file"]).read_bytes()
assert hashlib.sha256(image).hexdigest() == source["sha256"]
assert len(image) == source["byte_length"]
assert request["segments"] == []
print(
    json.dumps(
        {
            "protocol_version": 1,
            "predicted_segments": [
                {
                    "segment_id": "predicted-label",
                    "region": {"x": 1, "y": 1, "width": 4, "height": 4},
                }
            ],
            "observations": [
                {
                    "type": "ocr",
                    "region": {"x": 1, "y": 1, "width": 4, "height": 4},
                    "confidence": 1.0,
                    "payload": {"text": "Fixture Device", "serial": "SN-77"},
                },
                {
                    "type": "barcode",
                    "region": {"x": 1, "y": 1, "width": 4, "height": 4},
                    "confidence": 1.0,
                    "payload": {"value": "0123456789012", "format": "EAN-13"},
                },
            ],
        }
    )
)
