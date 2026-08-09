# Capture adapter protocol v1

A capture adapter is a trusted local executable selected by name from the
server-owned registry. The caller cannot provide argv, environment variables or
an image path. The runtime starts the exact configured argv in a private neutral
directory, writes the exact manifest-bound overview as `overview-image`, and
sends one JSON request on standard input.

The registry is version 2 and binds every adapter name to both an exact command
and a non-empty server-owned `revision`:

```json
{
  "version": 2,
  "adapters": {
    "ocr": {
      "command": ["/absolute/executable", "--fixed-option"],
      "revision": "sha256:model-and-code-release"
    }
  }
}
```

The operator must change `revision` whenever code, model weights, or any other
result-affecting implementation changes without changing argv. The immutable
capture artifact retains adapter name, revision, and command digest. This makes
lost-response retries revision-safe and leaves portable provenance after runtime
staging is retired following a verified apply.
Interrupted retirement is resumed by the private CLI `capture-cleanup` command,
which compares every remaining filename, byte count and digest with that durable
provenance. Read-profile status and MCP tools never delete staging.

## Request

```json
{
  "protocol_version": 1,
  "source": {
    "source_id": "source-...",
    "sha256": "64 lowercase hex characters",
    "byte_length": 12345,
    "content_type": "image/jpeg",
    "image_width": 3024,
    "image_height": 4032,
    "coordinate_space": "exif_transposed_pixels",
    "image_file": "overview-image"
  },
  "segments": [
    {
      "segment_id": "caller-label-1",
      "region": {"x": 10, "y": 20, "width": 300, "height": 120}
    }
  ]
}
```

`segments` is either the caller's exact checked rectangles or an empty list
requesting adapter segmentation. Every coordinate uses the image after EXIF
orientation is applied. The adapter receives the original file bytes and must
perform that transpose itself before reading pixels.

## Response

Legacy OCR/barcode-only adapters may return exactly `protocol_version` and
`observations`. A segmenting adapter adds `predicted_segments`:

```json
{
  "protocol_version": 1,
  "predicted_segments": [
    {
      "segment_id": "adapter-object-1",
      "region": {"x": 12, "y": 18, "width": 298, "height": 124}
    }
  ],
  "observations": [
    {
      "type": "ocr",
      "region": {"x": 20, "y": 30, "width": 180, "height": 40},
      "confidence": 0.91,
      "payload": {
        "text": "Model AB-1",
        "model_identifier": "AB-1",
        "serial": null
      }
    },
    {
      "type": "barcode",
      "region": {"x": 20, "y": 80, "width": 200, "height": 60},
      "confidence": 0.98,
      "payload": {"value": "0123456789012", "format": "EAN-13"}
    }
  ]
}
```

The response object has no other top-level keys. `predicted_segments` is
optional, but an empty caller request cannot prepare a capture unless the
adapter returns at least one prediction. Explicit caller segments always take
precedence. Observation payloads may retain additional JSON fields; OCR requires
`text` as string or null and barcode requires `value` as string or null.
`model_identifier` is optional string or null and is the only OCR field used for
exact model-identifier matching. Unknowns remain null or explicit `unknown`.

## Enforced limits and trust boundary

- Source: 64 MiB compressed, 80 million decoded pixels.
- Response: 64 KiB, JSON depth 32, no duplicate keys or non-finite numbers.
- Predicted segments: at most 256, unique non-empty IDs, exact rectangle fields,
  inside the post-EXIF image, aggregate area no more than four image areas.
- Generated crop: at most 64 MiB each and 256 MiB total.
- Execution: finite positive timeout, at most 60 seconds; timeout kills the
  adapter process group.
- Process: minimal environment, neutral private working directory, standard
  output only, bounded before JSON parsing.

The executable is trusted local code, not a sandbox. It may access the network
or other resources available to its operating-system account. The protocol
bounds what the inventory accepts; it does not confine the adapter.

Adapter output never establishes identity, possession, condition or location.
Preparation stores immutable crops and candidates; a separate digest-bound
review selects any item links before the ordinary verified proposal writer can
apply them.
