import json
import os
import sys

json.load(sys.stdin)
print(
    json.dumps(
        {
            "protocol_version": 1,
            "observations": [
                {
                    "type": "ocr",
                    "region": {"x": 0, "y": 0, "width": 1, "height": 1},
                    "confidence": 1.0,
                    "payload": {
                        "text": os.environ.get("INVENTORY_ADAPTER_SECRET", "absent"),
                        "cwd": os.getcwd(),
                    },
                }
            ],
        }
    )
)
