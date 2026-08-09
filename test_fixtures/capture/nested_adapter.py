import json

payload: object = "nested"
for _ in range(40):
    payload = [payload]
print(
    json.dumps(
        {
            "protocol_version": 1,
            "observations": [
                {
                    "type": "ocr",
                    "region": {"x": 0, "y": 0, "width": 1, "height": 1},
                    "confidence": 1.0,
                    "payload": {"text": "valid", "nested": payload},
                }
            ],
        }
    )
)
