import json
import sys

request = json.load(sys.stdin)
assert request["source"]["coordinate_space"] == "exif_transposed_pixels"
print(
    json.dumps(
        {
            "protocol_version": 1,
            "observations": [
                {
                    "type": "ocr",
                    "region": {"x": 0, "y": 0, "width": 1, "height": 1},
                    "confidence": 0.99,
                    "payload": {
                        "text": "No shared display tokens",
                        "model_identifier": "IDENTIFIER-ONLY-42",
                    },
                }
            ],
        }
    )
)
