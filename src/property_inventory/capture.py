"""Pure, local primitives for reviewable overview-image capture.

This module deliberately does not know about the inventory store. It creates
immutable descriptions and observations which an integration layer may later
turn into a proposal after human review.
"""

from __future__ import annotations

import hashlib
import io
import math
from collections.abc import Callable, Iterable, Mapping
from dataclasses import dataclass
from types import MappingProxyType
from typing import Any


class CaptureError(ValueError):
    """Raised when capture input is incomplete, malformed, or unsafe."""


class CaptureDependencyError(CaptureError):
    """Raised when deterministic image cropping needs the optional Pillow extra."""


JsonValue = str | int | float | bool | None | tuple["JsonValue", ...] | Mapping[str, "JsonValue"]

# Product limits, independent of Pillow defaults. They bound compressed input
# and decoded memory before one overview can be amplified into many crops.
MAX_CAPTURE_SOURCE_BYTES = 64 * 1024 * 1024
MAX_CAPTURE_PIXELS = 80_000_000
MAX_CAPTURE_SEGMENTS = 256
MAX_CAPTURE_CROP_BYTES = 64 * 1024 * 1024
MAX_CAPTURE_TOTAL_CROP_BYTES = 256 * 1024 * 1024
MAX_CAPTURE_JSON_DEPTH = 32
MAX_CAPTURE_JSON_NODES = 100_000
CAPTURE_COORDINATE_SPACE = "exif_transposed_pixels"


def _required_text(value: object, field: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise CaptureError(f"{field} must be a non-empty string")
    return value


def _positive_int(value: object, field: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
        raise CaptureError(f"{field} must be a positive integer")
    return value


def _non_negative_int(value: object, field: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise CaptureError(f"{field} must be a non-negative integer")
    return value


def _sha256(value: object, field: str) -> str:
    if not isinstance(value, str) or len(value) != 64:
        raise CaptureError(f"{field} must be a SHA-256 hex digest")
    try:
        int(value, 16)
    except ValueError as error:
        raise CaptureError(f"{field} must be a SHA-256 hex digest") from error
    return value.lower()


def validate_json_value(
    value: object,
    field: str = "value",
    *,
    max_depth: int = MAX_CAPTURE_JSON_DEPTH,
) -> None:
    """Bound JSON shape iteratively before any recursive freezing or thawing."""
    stack: list[tuple[object, int]] = [(value, 0)]
    seen_containers: set[int] = set()
    node_count = 0
    while stack:
        current, depth = stack.pop()
        node_count += 1
        if node_count > MAX_CAPTURE_JSON_NODES:
            raise CaptureError(f"{field} exceeds the JSON node limit")
        if depth > max_depth:
            raise CaptureError(f"{field} exceeds the JSON depth limit")
        if current is None or isinstance(current, (str, int, bool)):
            continue
        if isinstance(current, float):
            if not math.isfinite(current):
                raise CaptureError(f"{field} must not contain non-finite numbers")
            continue
        if isinstance(current, Mapping):
            identity = id(current)
            if identity in seen_containers:
                raise CaptureError(f"{field} must be a JSON tree without cycles or aliases")
            seen_containers.add(identity)
            for key, child in current.items():
                if not isinstance(key, str):
                    raise CaptureError(f"{field} mapping keys must be strings")
                stack.append((child, depth + 1))
            continue
        if isinstance(current, (list, tuple)):
            identity = id(current)
            if identity in seen_containers:
                raise CaptureError(f"{field} must be a JSON tree without cycles or aliases")
            seen_containers.add(identity)
            stack.extend((child, depth + 1) for child in current)
            continue
        raise CaptureError(f"{field} must contain JSON-compatible values")


def _freeze_json_unchecked(value: object, field: str) -> JsonValue:
    if value is None or isinstance(value, (str, int, bool)):
        return value
    if isinstance(value, float):
        return value
    if isinstance(value, Mapping):
        frozen: dict[str, JsonValue] = {}
        for key, child in value.items():
            frozen[key] = _freeze_json_unchecked(child, field)
        return MappingProxyType(frozen)
    if isinstance(value, (list, tuple)):
        return tuple(_freeze_json_unchecked(child, field) for child in value)
    raise AssertionError("validated JSON shape changed while freezing")


def _freeze_json(value: object, field: str = "value") -> JsonValue:
    validate_json_value(value, field)
    return _freeze_json_unchecked(value, field)


def thaw_json(value: JsonValue) -> Any:
    """Return a normal JSON-compatible value without exposing frozen internals."""
    if isinstance(value, Mapping):
        return {key: thaw_json(child) for key, child in value.items()}
    if isinstance(value, tuple):
        return [thaw_json(child) for child in value]
    return value


@dataclass(frozen=True)
class ImageRegion:
    """A pixel-aligned rectangle relative to an overview image."""

    x: int
    y: int
    width: int
    height: int

    def __post_init__(self) -> None:
        _non_negative_int(self.x, "region.x")
        _non_negative_int(self.y, "region.y")
        _positive_int(self.width, "region.width")
        _positive_int(self.height, "region.height")

    @property
    def right(self) -> int:
        return self.x + self.width

    @property
    def bottom(self) -> int:
        return self.y + self.height

    def validate_within(self, image_width: int, image_height: int) -> None:
        _positive_int(image_width, "image_width")
        _positive_int(image_height, "image_height")
        if self.right > image_width or self.bottom > image_height:
            raise CaptureError("region is outside overview image bounds")

    def to_dict(self) -> dict[str, int]:
        return {"x": self.x, "y": self.y, "width": self.width, "height": self.height}

    @classmethod
    def from_mapping(cls, value: Mapping[str, object], field: str = "region") -> ImageRegion:
        if set(value) != {"x", "y", "width", "height"}:
            raise CaptureError(f"{field} must contain exactly x, y, width, and height")
        return cls(
            x=_non_negative_int(value["x"], f"{field}.x"),
            y=_non_negative_int(value["y"], f"{field}.y"),
            width=_positive_int(value["width"], f"{field}.width"),
            height=_positive_int(value["height"], f"{field}.height"),
        )


@dataclass(frozen=True)
class CaptureSegment:
    """A named bounded area supplied by an agent or a human reviewer."""

    segment_id: str
    region: ImageRegion

    def __post_init__(self) -> None:
        _required_text(self.segment_id, "segment_id")
        if not isinstance(self.region, ImageRegion):
            raise CaptureError("segment.region must be an ImageRegion")

    def to_dict(self) -> dict[str, object]:
        return {"segment_id": self.segment_id, "region": self.region.to_dict()}


@dataclass(frozen=True)
class SourceManifest:
    """Immutable metadata for the original overview bytes."""

    source_id: str
    sha256: str
    byte_length: int
    content_type: str
    image_width: int
    image_height: int
    coordinate_space: str = CAPTURE_COORDINATE_SPACE

    def __post_init__(self) -> None:
        _required_text(self.source_id, "source_id")
        object.__setattr__(self, "sha256", _sha256(self.sha256, "sha256"))
        _positive_int(self.byte_length, "byte_length")
        if self.byte_length > MAX_CAPTURE_SOURCE_BYTES:
            raise CaptureError("source image exceeds the capture byte limit")
        _required_text(self.content_type, "content_type")
        _positive_int(self.image_width, "image_width")
        _positive_int(self.image_height, "image_height")
        if self.coordinate_space != CAPTURE_COORDINATE_SPACE:
            raise CaptureError(
                f"coordinate_space must be {CAPTURE_COORDINATE_SPACE!r}"
            )
        if self.image_width * self.image_height > MAX_CAPTURE_PIXELS:
            raise CaptureError("source image exceeds the capture pixel limit")

    def to_dict(self) -> dict[str, object]:
        return {
            "source_id": self.source_id,
            "sha256": self.sha256,
            "byte_length": self.byte_length,
            "content_type": self.content_type,
            "image_width": self.image_width,
            "image_height": self.image_height,
            "coordinate_space": self.coordinate_space,
        }


def _crop_identifier(
    *, source_sha256: str, segment_id: str, region: ImageRegion, crop_sha256: str
) -> str:
    binding = "\x00".join(
        (
            source_sha256,
            segment_id,
            str(region.x),
            str(region.y),
            str(region.width),
            str(region.height),
            crop_sha256,
        )
    )
    return f"crop-{hashlib.sha256(binding.encode('utf-8')).hexdigest()}"


@dataclass(frozen=True)
class CropManifest:
    """Immutable metadata linking a deterministic crop to its exact overview region."""

    crop_id: str
    source_id: str
    source_sha256: str
    segment_id: str
    region: ImageRegion
    sha256: str
    byte_length: int
    content_type: str = "image/png"

    def __post_init__(self) -> None:
        _required_text(self.crop_id, "crop_id")
        _required_text(self.source_id, "source_id")
        _required_text(self.segment_id, "segment_id")
        if not isinstance(self.region, ImageRegion):
            raise CaptureError("crop.region must be an ImageRegion")
        source_sha256 = _sha256(self.source_sha256, "source_sha256")
        crop_sha256 = _sha256(self.sha256, "sha256")
        object.__setattr__(self, "source_sha256", source_sha256)
        object.__setattr__(self, "sha256", crop_sha256)
        expected_id = _crop_identifier(
            source_sha256=source_sha256,
            segment_id=self.segment_id,
            region=self.region,
            crop_sha256=crop_sha256,
        )
        if self.crop_id != expected_id:
            raise CaptureError("crop_id must bind source digest, region, and crop digest")
        _positive_int(self.byte_length, "byte_length")
        if self.content_type != "image/png":
            raise CaptureError("crop content_type must be image/png")

    def to_dict(self) -> dict[str, object]:
        return {
            "crop_id": self.crop_id,
            "source_id": self.source_id,
            "source_sha256": self.source_sha256,
            "segment_id": self.segment_id,
            "region": self.region.to_dict(),
            "sha256": self.sha256,
            "byte_length": self.byte_length,
            "content_type": self.content_type,
        }


@dataclass(frozen=True)
class OverviewCaptureSession:
    """Validated overview capture request, with every segment inside its bounds."""

    session_id: str
    captured_on: str
    source: SourceManifest
    segments: tuple[CaptureSegment, ...]

    def __post_init__(self) -> None:
        _required_text(self.session_id, "session_id")
        _required_text(self.captured_on, "captured_on")
        if not isinstance(self.source, SourceManifest):
            raise CaptureError("capture session source must be a SourceManifest")
        if type(self.segments) is not tuple or not self.segments:
            raise CaptureError("capture session segments must be a non-empty tuple")
        if len(self.segments) > MAX_CAPTURE_SEGMENTS:
            raise CaptureError("capture session exceeds the segment limit")
        if any(not isinstance(segment, CaptureSegment) for segment in self.segments):
            raise CaptureError("capture session segments must be CaptureSegment values")
        ids = [segment.segment_id for segment in self.segments]
        if len(ids) != len(set(ids)):
            raise CaptureError("capture session segment_id values must be unique")
        for segment in self.segments:
            segment.region.validate_within(self.source.image_width, self.source.image_height)

    def to_dict(self) -> dict[str, object]:
        return {
            "session_id": self.session_id,
            "captured_on": self.captured_on,
            "source": self.source.to_dict(),
            "segments": [segment.to_dict() for segment in self.segments],
        }


@dataclass(frozen=True)
class GeneratedCrop:
    """Deterministic PNG crop bytes with an immutable manifest."""

    manifest: CropManifest
    data: bytes

    def __post_init__(self) -> None:
        if not isinstance(self.manifest, CropManifest):
            raise CaptureError("crop manifest must be a CropManifest")
        if not isinstance(self.data, bytes) or not self.data:
            raise CaptureError("crop data must be non-empty bytes")
        if self.manifest.byte_length != len(self.data):
            raise CaptureError("crop manifest byte_length does not match data")
        if self.manifest.sha256 != hashlib.sha256(self.data).hexdigest():
            raise CaptureError("crop manifest sha256 does not match data")


def make_source_manifest(
    *,
    source_id: str,
    data: bytes,
    content_type: str,
    image_width: int,
    image_height: int,
) -> SourceManifest:
    """Make source metadata before a session is persisted by a higher layer."""
    if not isinstance(data, bytes) or not data:
        raise CaptureError("source data must be non-empty bytes")
    return SourceManifest(
        source_id=source_id,
        sha256=hashlib.sha256(data).hexdigest(),
        byte_length=len(data),
        content_type=content_type,
        image_width=image_width,
        image_height=image_height,
    )


def _pillow_loader(data: bytes) -> Any:
    try:
        from PIL import Image, ImageOps
    except ImportError as error:  # pragma: no cover - depends on optional installation.
        raise CaptureDependencyError(
            "deterministic cropping requires Pillow; install the capture image dependency"
        ) from error
    try:
        image = Image.open(io.BytesIO(data))
        width, height = image.size
        if width * height > MAX_CAPTURE_PIXELS:
            image.close()
            raise CaptureError("source image exceeds the capture pixel limit")
        image.load()
        oriented = ImageOps.exif_transpose(image)
    except CaptureError:
        raise
    except (Image.DecompressionBombError, OSError, ValueError) as error:
        raise CaptureError("source image is not safely decodable") from error
    if oriented is not image:
        image.close()
    return oriented


def generate_crop(
    *,
    source: SourceManifest,
    source_data: bytes,
    segment: CaptureSegment,
    image_loader: Callable[[bytes], Any] | None = None,
) -> GeneratedCrop:
    """Crop an overview deterministically as PNG, without writing any bytes to disk."""
    if not isinstance(source, SourceManifest) or not isinstance(segment, CaptureSegment):
        raise CaptureError("source and segment must be immutable capture values")
    if not isinstance(source_data, bytes) or not source_data:
        raise CaptureError("source_data must be non-empty bytes")
    if len(source_data) > MAX_CAPTURE_SOURCE_BYTES:
        raise CaptureError("source image exceeds the capture byte limit")
    if len(source_data) != source.byte_length:
        raise CaptureError("source_data length does not match source manifest")
    if hashlib.sha256(source_data).hexdigest() != source.sha256:
        raise CaptureError("source_data hash does not match source manifest")
    segment.region.validate_within(source.image_width, source.image_height)
    image = (image_loader or _pillow_loader)(source_data)
    try:
        if getattr(image, "size", None) != (source.image_width, source.image_height):
            raise CaptureError("source manifest dimensions do not match decoded image")
        crop = image.crop((segment.region.x, segment.region.y, segment.region.right, segment.region.bottom))
        output = io.BytesIO()
        crop.save(output, format="PNG", optimize=False)
        crop_bytes = output.getvalue()
        if len(crop_bytes) > MAX_CAPTURE_CROP_BYTES:
            raise CaptureError("generated crop exceeds the capture byte limit")
    finally:
        close = getattr(image, "close", None)
        if callable(close):
            close()
    crop_hash = hashlib.sha256(crop_bytes).hexdigest()
    manifest = CropManifest(
        crop_id=_crop_identifier(
            source_sha256=source.sha256,
            segment_id=segment.segment_id,
            region=segment.region,
            crop_sha256=crop_hash,
        ),
        source_id=source.source_id,
        source_sha256=source.sha256,
        segment_id=segment.segment_id,
        region=segment.region,
        sha256=crop_hash,
        byte_length=len(crop_bytes),
    )
    return GeneratedCrop(manifest=manifest, data=crop_bytes)


@dataclass(frozen=True)
class CaptureObservation:
    """An immutable OCR or barcode observation, including uncertain source fields."""

    observation_type: str
    region: ImageRegion
    confidence: float | None
    payload: Mapping[str, JsonValue]

    def __post_init__(self) -> None:
        if self.observation_type not in {"ocr", "barcode"}:
            raise CaptureError("observation_type must be ocr or barcode")
        if not isinstance(self.region, ImageRegion):
            raise CaptureError("observation.region must be an ImageRegion")
        if self.confidence is not None:
            if isinstance(self.confidence, bool) or not isinstance(self.confidence, (int, float)):
                raise CaptureError("confidence must be a number or null")
            if not math.isfinite(float(self.confidence)) or not 0 <= float(self.confidence) <= 1:
                raise CaptureError("confidence must be finite and between 0 and 1")
            object.__setattr__(self, "confidence", float(self.confidence))
        if not isinstance(self.payload, Mapping):
            raise CaptureError("payload must be a mapping")
        payload = _freeze_json(self.payload, "observation.payload")
        if not isinstance(payload, Mapping):
            raise CaptureError("payload must be a mapping")
        object.__setattr__(self, "payload", payload)

    def to_dict(self) -> dict[str, object]:
        return {
            "type": self.observation_type,
            "region": self.region.to_dict(),
            "confidence": self.confidence,
            "payload": thaw_json(self.payload),
        }


def normalize_observation(
    raw: Mapping[str, object], *, image_width: int | None = None, image_height: int | None = None
) -> CaptureObservation:
    """Validate an adapter observation without replacing unknown values or fields."""
    if not isinstance(raw, Mapping):
        raise CaptureError("observation must be an object")
    if set(raw) != {"type", "region", "confidence", "payload"}:
        raise CaptureError("observation must contain exactly type, region, confidence, and payload")
    observation_type = _required_text(raw["type"], "observation.type")
    region_raw = raw["region"]
    payload_raw = raw["payload"]
    if not isinstance(region_raw, Mapping):
        raise CaptureError("observation.region must be an object")
    if not isinstance(payload_raw, Mapping):
        raise CaptureError("observation.payload must be an object")
    region = ImageRegion.from_mapping(region_raw, "observation.region")
    if image_width is not None or image_height is not None:
        if image_width is None or image_height is None:
            raise CaptureError("both image dimensions are required to validate observation bounds")
        region.validate_within(image_width, image_height)
    if observation_type == "ocr":
        text = payload_raw.get("text")
        if "text" not in payload_raw or (text is not None and not isinstance(text, str)):
            raise CaptureError("ocr observation.payload.text must be a string or null")
        model_identifier = payload_raw.get("model_identifier")
        if model_identifier is not None and not isinstance(model_identifier, str):
            raise CaptureError(
                "ocr observation.payload.model_identifier must be a string or null"
            )
    if observation_type == "barcode":
        value = payload_raw.get("value")
        if "value" not in payload_raw or (value is not None and not isinstance(value, str)):
            raise CaptureError("barcode observation.payload.value must be a string or null")
    return CaptureObservation(
        observation_type=observation_type,
        region=region,
        confidence=raw["confidence"],
        payload=payload_raw,
    )


def normalize_observations(
    raw_observations: Iterable[Mapping[str, object]], *, image_width: int | None = None, image_height: int | None = None
) -> tuple[CaptureObservation, ...]:
    """Normalize a finite adapter output while retaining each unknown as supplied."""
    return tuple(
        normalize_observation(raw, image_width=image_width, image_height=image_height)
        for raw in raw_observations
    )


def _is_unknown(value: object) -> bool:
    return value is None or (
        isinstance(value, str) and value.strip().casefold() == "unknown"
    )


def _intersection_over_union(left: ImageRegion, right: ImageRegion) -> float:
    intersection_width = max(0, min(left.right, right.right) - max(left.x, right.x))
    intersection_height = max(0, min(left.bottom, right.bottom) - max(left.y, right.y))
    intersection = intersection_width * intersection_height
    union = left.width * left.height + right.width * right.height - intersection
    return intersection / union if union else 0.0


@dataclass(frozen=True)
class BenchmarkMetric:
    """A metric with denominator, errors, unknown truth, and model abstentions explicit."""

    correct: int
    denominator: int
    errors: tuple[str, ...]
    unknowns: int
    abstentions: int = 0

    def __post_init__(self) -> None:
        if any(
            isinstance(value, bool) or not isinstance(value, int) or value < 0
            for value in (self.correct, self.denominator, self.unknowns, self.abstentions)
        ):
            raise CaptureError("benchmark metric counts must be non-negative integers")
        if self.correct > self.denominator:
            raise CaptureError("benchmark metric correct count exceeds denominator")
        if type(self.errors) is not tuple or any(not isinstance(error, str) for error in self.errors):
            raise CaptureError("benchmark metric errors must be a tuple of strings")

    @property
    def value(self) -> float | None:
        return self.correct / self.denominator if self.denominator else None

    def to_dict(self) -> dict[str, object]:
        return {
            "value": self.value,
            "correct": self.correct,
            "denominator": self.denominator,
            "errors": list(self.errors),
            "unknowns": self.unknowns,
            "abstentions": self.abstentions,
        }


@dataclass(frozen=True)
class CaptureBenchmarkReport:
    """Fixture-grounded report that never upgrades synthetic results into real-room proof."""

    corpus_label: str
    claim: str
    segmentation_iou: float | None
    segmentation_precision: BenchmarkMetric
    segmentation_recall: BenchmarkMetric
    field_exact_match: BenchmarkMetric
    barcode_exact_match: BenchmarkMetric
    duplicate_top_1: BenchmarkMetric
    duplicate_top_k: BenchmarkMetric

    def to_dict(self) -> dict[str, object]:
        return {
            "corpus_label": self.corpus_label,
            "claim": self.claim,
            "segmentation_iou": self.segmentation_iou,
            "segmentation_precision": self.segmentation_precision.to_dict(),
            "segmentation_recall": self.segmentation_recall.to_dict(),
            "field_exact_match": self.field_exact_match.to_dict(),
            "barcode_exact_match": self.barcode_exact_match.to_dict(),
            "duplicate_top_1": self.duplicate_top_1.to_dict(),
            "duplicate_top_k": self.duplicate_top_k.to_dict(),
        }


def _metric_from_pairs(pairs: Iterable[tuple[str, object, object]]) -> BenchmarkMetric:
    correct = 0
    unknowns = 0
    abstentions = 0
    errors: list[str] = []
    denominator = 0
    for label, expected, actual in pairs:
        if _is_unknown(expected):
            unknowns += 1
            continue
        denominator += 1
        if _is_unknown(actual):
            abstentions += 1
            errors.append(label)
        elif expected == actual:
            correct += 1
        else:
            errors.append(label)
    return BenchmarkMetric(correct, denominator, tuple(errors), unknowns, abstentions)


def _regions(raw: object, field: str) -> tuple[ImageRegion, ...]:
    if not isinstance(raw, list):
        raise CaptureError(f"{field} must be a list")
    regions: list[ImageRegion] = []
    for index, region in enumerate(raw):
        if not isinstance(region, Mapping):
            raise CaptureError(f"{field}[{index}] must be an object")
        regions.append(ImageRegion.from_mapping(region, f"{field}[{index}]"))
    return tuple(regions)


def _match_regions(
    predicted: tuple[ImageRegion, ...], truth: tuple[ImageRegion, ...], threshold: float
) -> tuple[tuple[float, int, int], ...]:
    """Return a deterministic maximum-cardinality bipartite matching above threshold."""
    adjacency = {
        predicted_index: sorted(
            (
                (_intersection_over_union(prediction, expected), truth_index)
                for truth_index, expected in enumerate(truth)
                if _intersection_over_union(prediction, expected) >= threshold
            ),
            key=lambda row: (-row[0], row[1]),
        )
        for predicted_index, prediction in enumerate(predicted)
    }
    matched_truth: dict[int, int] = {}

    def augment(predicted_index: int, seen_truth: set[int]) -> bool:
        for _, truth_index in adjacency[predicted_index]:
            if truth_index in seen_truth:
                continue
            seen_truth.add(truth_index)
            prior = matched_truth.get(truth_index)
            if prior is None or augment(prior, seen_truth):
                matched_truth[truth_index] = predicted_index
                return True
        return False

    for predicted_index in range(len(predicted)):
        augment(predicted_index, set())
    pairs = [
        (_intersection_over_union(predicted[predicted_index], truth[truth_index]), predicted_index, truth_index)
        for truth_index, predicted_index in matched_truth.items()
    ]
    return tuple(sorted(pairs, key=lambda row: (row[1], row[2])))


def run_capture_benchmark(
    *,
    cases: Iterable[Mapping[str, object]],
    corpus_label: str,
    provenance: Mapping[str, object] | None = None,
    top_k: int = 3,
    segmentation_iou_threshold: float = 0.5,
) -> CaptureBenchmarkReport:
    """Evaluate a non-empty corpus with explicit abstentions and provenance-limited labels."""
    claims = {
        "synthetic": "synthetic-fixture-only",
        "manual-fixture": "manual-fixture-only",
        "real-room": "real-room-sample-not-statistical-proof",
    }
    if corpus_label not in claims:
        choices = ", ".join(sorted(claims))
        raise CaptureError(f"corpus_label must be one of {choices}")
    if corpus_label == "real-room":
        if not isinstance(provenance, Mapping) or provenance.get("manually_checked") is not True:
            raise CaptureError("real-room corpus_label requires provenance.manually_checked=true")
    if isinstance(top_k, bool) or not isinstance(top_k, int) or top_k <= 0:
        raise CaptureError("top_k must be a positive integer")
    if (
        isinstance(segmentation_iou_threshold, bool)
        or not isinstance(segmentation_iou_threshold, (int, float))
        or not math.isfinite(float(segmentation_iou_threshold))
        or not 0 < segmentation_iou_threshold <= 1
    ):
        raise CaptureError("segmentation_iou_threshold must be finite and in (0, 1]")
    case_rows = tuple(cases)
    if not case_rows:
        raise CaptureError("benchmark corpus must contain at least one case")
    predicted_total = 0
    truth_total = 0
    matched_ious: list[float] = []
    segmentation_precision_errors: list[str] = []
    segmentation_recall_errors: list[str] = []
    field_pairs: list[tuple[str, object, object]] = []
    barcode_pairs: list[tuple[str, object, object]] = []
    duplicate_top_one_pairs: list[tuple[str, object, object]] = []
    duplicate_top_k_pairs: list[tuple[str, object, object]] = []
    for index, case in enumerate(case_rows):
        if not isinstance(case, Mapping):
            raise CaptureError(f"benchmark case {index} must be an object")
        case_id = _required_text(case.get("case_id", f"case-{index}"), f"benchmark case {index}.case_id")
        predicted = _regions(case.get("predicted_segments", []), f"{case_id}.predicted_segments")
        truth = _regions(case.get("truth_segments", []), f"{case_id}.truth_segments")
        predicted_total += len(predicted)
        truth_total += len(truth)
        region_matches = _match_regions(predicted, truth, segmentation_iou_threshold)
        matched_ious.extend(match[0] for match in region_matches)
        matched_prediction_indexes = {match[1] for match in region_matches}
        matched_truth_indexes = {match[2] for match in region_matches}
        segmentation_precision_errors.extend(
            f"{case_id}:extra-segment:{segment_index}"
            for segment_index in sorted(set(range(len(predicted))) - matched_prediction_indexes)
        )
        segmentation_recall_errors.extend(
            f"{case_id}:missed-segment:{segment_index}"
            for segment_index in sorted(set(range(len(truth))) - matched_truth_indexes)
        )
        expected_fields = case.get("expected_fields", {})
        observed_fields = case.get("observed_fields", {})
        if not isinstance(expected_fields, Mapping) or not isinstance(observed_fields, Mapping):
            raise CaptureError(f"{case_id} field expectations and observations must be objects")
        for key in sorted(set(expected_fields) | set(observed_fields)):
            if not isinstance(key, str):
                raise CaptureError(f"{case_id} field names must be strings")
            field_pairs.append((f"{case_id}:field:{key}", expected_fields.get(key), observed_fields.get(key)))
        barcode_pairs.append(
            (f"{case_id}:barcode", case.get("expected_barcode"), case.get("observed_barcode"))
        )
        expected_duplicate = case.get("expected_duplicate_id")
        ranked_duplicates = case.get("ranked_duplicate_ids", "unknown")
        if ranked_duplicates == "unknown" or ranked_duplicates is None:
            duplicate_top_one_pairs.append((f"{case_id}:duplicate-top-1", expected_duplicate, "unknown"))
            duplicate_top_k_pairs.append((f"{case_id}:duplicate-top-{top_k}", expected_duplicate, "unknown"))
        else:
            if not isinstance(ranked_duplicates, list) or any(
                not isinstance(value, str) for value in ranked_duplicates
            ):
                raise CaptureError(f"{case_id}.ranked_duplicate_ids must be a string list or unknown")
            duplicate_top_one_pairs.append(
                (
                    f"{case_id}:duplicate-top-1",
                    expected_duplicate,
                    ranked_duplicates[0] if ranked_duplicates else "<no-match>",
                )
            )
            duplicate_top_k_pairs.append(
                (
                    f"{case_id}:duplicate-top-{top_k}",
                    expected_duplicate,
                    expected_duplicate if expected_duplicate in ranked_duplicates[:top_k] else "<no-match>",
                )
            )
    segmentation_precision = BenchmarkMetric(
        correct=len(matched_ious),
        denominator=predicted_total,
        errors=tuple(segmentation_precision_errors),
        unknowns=0,
    )
    segmentation_recall = BenchmarkMetric(
        correct=len(matched_ious),
        denominator=truth_total,
        errors=tuple(segmentation_recall_errors),
        unknowns=0,
    )
    return CaptureBenchmarkReport(
        corpus_label=corpus_label,
        claim=claims[corpus_label],
        segmentation_iou=sum(matched_ious) / len(matched_ious) if matched_ious else None,
        segmentation_precision=segmentation_precision,
        segmentation_recall=segmentation_recall,
        field_exact_match=_metric_from_pairs(field_pairs),
        barcode_exact_match=_metric_from_pairs(barcode_pairs),
        duplicate_top_1=_metric_from_pairs(duplicate_top_one_pairs),
        duplicate_top_k=_metric_from_pairs(duplicate_top_k_pairs),
    )
