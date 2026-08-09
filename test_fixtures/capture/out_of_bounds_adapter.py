import json

print(
    json.dumps(
        {
            "protocol_version": 1,
            "observations": [
                {
                    "type": "ocr",
                    "region": {"x": 9, "y": 0, "width": 2, "height": 1},
                    "confidence": 0.5,
                    "payload": {"text": "valid type, invalid region"},
                }
            ],
        }
    )
)
