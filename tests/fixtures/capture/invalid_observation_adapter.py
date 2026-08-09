import json

print(
    json.dumps(
        {
            "protocol_version": 1,
            "observations": [
                {
                    "type": "ocr",
                    "region": {"x": 0, "y": 0, "width": 1, "height": 1},
                    "confidence": 0.5,
                    "payload": {"text": 12},
                }
            ],
        }
    )
)
