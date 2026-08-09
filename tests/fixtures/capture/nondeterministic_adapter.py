import json
import sys
import uuid

request = json.load(sys.stdin)
assert request["protocol_version"] == 1
print(
    json.dumps(
        {
            "protocol_version": 1,
            "observations": [
                {
                    "type": "ocr",
                    "region": {"x": 0, "y": 0, "width": 1, "height": 1},
                    "confidence": None,
                    "payload": {"text": str(uuid.uuid4())},
                }
            ],
        }
    )
)
