"""Store-facing capture preparation and materialisation helpers.

Capture is deliberately two phase. Preparation copies exact bytes into the
private runtime and writes a digest-bound artifact for review. Review creates
the proposal. Materialisation is only called from the normal proposal
transaction, so observations never become
ownership, condition, location, or possession assertions by accident.
"""

from __future__ import annotations

import base64
import hashlib
import json
import os
import shutil
import stat
import uuid
from collections.abc import Mapping
from pathlib import Path
from typing import Any

from .capture import (
    MAX_CAPTURE_PIXELS,
    MAX_CAPTURE_SEGMENTS,
    MAX_CAPTURE_SOURCE_BYTES,
    MAX_CAPTURE_TOTAL_CROP_BYTES,
    CaptureBenchmarkReport,
    CaptureError,
    CaptureSegment,
    ImageRegion,
    SourceManifest,
    generate_crop,
    make_source_manifest,
    run_capture_benchmark,
)
from .capture_adapters import AdapterRegistry, AdapterResponse, run_local_adapter
from .capture_provenance import capture_session_id_for_artifact
from .duplicates import DuplicateError, DuplicateSubject, rank_duplicate_candidates


class CaptureServiceError(ValueError):
    """Raised for an unsafe or malformed capture integration request."""


MAX_DUPLICATE_CANDIDATES_PER_OBSERVATION = 5
MAX_CAPTURE_STAGING_METADATA_BYTES = 8 * 1024 * 1024
_CAPTURE_BUILD_PREFIX = ".capture-build-"
CAPTURE_REQUEST_DIGEST_FILE = "request.sha256"


def _digest(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _canonical(value: object) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False)


def _fsync_directory(path: Path) -> None:
    descriptor = os.open(
        path,
        os.O_RDONLY | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_DIRECTORY", 0),
    )
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def _write_private_file(path: Path, payload: bytes) -> None:
    descriptor = os.open(
        path,
        os.O_WRONLY
        | os.O_CREAT
        | os.O_EXCL
        | getattr(os, "O_CLOEXEC", 0)
        | getattr(os, "O_NOFOLLOW", 0),
        0o600,
    )
    os.fchmod(descriptor, 0o600)
    with os.fdopen(descriptor, "wb") as handle:
        handle.write(payload)
        handle.flush()
        os.fsync(handle.fileno())


def _read_exact_private_file(path: Path, expected: bytes) -> bytes:
    flags = (
        os.O_RDONLY
        | getattr(os, "O_CLOEXEC", 0)
        | getattr(os, "O_NOFOLLOW", 0)
        | getattr(os, "O_NONBLOCK", 0)
    )
    try:
        descriptor = os.open(path, flags)
    except OSError as error:
        raise CaptureServiceError(
            "existing deterministic capture is missing or unsafe"
        ) from error
    try:
        metadata = os.fstat(descriptor)
        if not stat.S_ISREG(metadata.st_mode) or metadata.st_size != len(expected):
            raise CaptureServiceError(
                "existing deterministic capture disagrees with preparation"
            )
        chunks: list[bytes] = []
        remaining = len(expected) + 1
        while remaining:
            chunk = os.read(descriptor, min(1024 * 1024, remaining))
            if not chunk:
                break
            chunks.append(chunk)
            remaining -= len(chunk)
        payload = b"".join(chunks)
    finally:
        os.close(descriptor)
    if payload != expected:
        raise CaptureServiceError(
            "existing deterministic capture disagrees with preparation"
        )
    return payload


def _read_bounded_private_file(path: Path, *, maximum_bytes: int) -> bytes:
    flags = (
        os.O_RDONLY
        | getattr(os, "O_CLOEXEC", 0)
        | getattr(os, "O_NOFOLLOW", 0)
        | getattr(os, "O_NONBLOCK", 0)
    )
    try:
        descriptor = os.open(path, flags)
    except OSError as error:
        raise CaptureServiceError(
            "existing deterministic capture is missing or unsafe"
        ) from error
    try:
        metadata = os.fstat(descriptor)
        if (
            not stat.S_ISREG(metadata.st_mode)
            or metadata.st_size < 1
            or metadata.st_size > maximum_bytes
        ):
            raise CaptureServiceError(
                "existing deterministic capture review is unsafe"
            )
        chunks: list[bytes] = []
        remaining = maximum_bytes + 1
        while remaining:
            chunk = os.read(descriptor, min(1024 * 1024, remaining))
            if not chunk:
                break
            chunks.append(chunk)
            remaining -= len(chunk)
    finally:
        os.close(descriptor)
    payload = b"".join(chunks)
    if not payload or len(payload) > maximum_bytes:
        raise CaptureServiceError("existing deterministic capture review is unsafe")
    return payload


def _validate_existing_preparation(
    stage_root: Path, expected_files: Mapping[str, bytes]
) -> None:
    if stage_root.is_symlink() or not stage_root.is_dir():
        raise CaptureServiceError("existing deterministic capture is unsafe")
    actual = {entry.name for entry in stage_root.iterdir()}
    allowed = set(expected_files)
    if actual not in (allowed, allowed | {"review.json"}):
        raise CaptureServiceError(
            "existing deterministic capture disagrees with preparation"
        )
    for name, payload in expected_files.items():
        _read_exact_private_file(stage_root / name, payload)
    if "review.json" in actual:
        _read_bounded_private_file(
            stage_root / "review.json",
            maximum_bytes=MAX_CAPTURE_STAGING_METADATA_BYTES,
        )


def _cleanup_abandoned_capture_builds(stage_parent: Path) -> None:
    removed = False
    for entry in stage_parent.iterdir():
        if not entry.name.startswith(_CAPTURE_BUILD_PREFIX):
            continue
        identifier = entry.name.removeprefix(_CAPTURE_BUILD_PREFIX)
        try:
            valid_identifier = str(uuid.UUID(identifier)) == identifier
        except ValueError:
            valid_identifier = False
        if not valid_identifier:
            continue
        if entry.is_symlink() or not entry.is_dir():
            raise CaptureServiceError("abandoned capture preparation is unsafe")
        shutil.rmtree(entry)
        removed = True
    if removed:
        _fsync_directory(stage_parent)


def _load_existing_capture_artifact(
    stage_root: Path, *, session_id: str, request_digest: str | None
) -> dict[str, object]:
    if stage_root.is_symlink() or not stage_root.is_dir():
        raise CaptureServiceError("existing deterministic capture is unsafe")
    payload = _read_bounded_private_file(
        stage_root / "artifact.json",
        maximum_bytes=MAX_CAPTURE_STAGING_METADATA_BYTES,
    )
    try:
        artifact = json.loads(payload.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as error:
        raise CaptureServiceError(
            "existing deterministic capture artifact is malformed"
        ) from error
    if (
        not isinstance(artifact, dict)
        or artifact.get("format") != 1
        or artifact.get("capture_session_id") != session_id
        or (
            request_digest is not None
            and artifact.get("request_digest") != request_digest
        )
        or capture_session_id_for_artifact(artifact) != session_id
    ):
        raise CaptureServiceError(
            "existing deterministic capture disagrees with preparation request"
        )
    return artifact


def _find_existing_capture_for_request(
    stage_parent: Path, *, request_digest: str
) -> dict[str, object] | None:
    match: dict[str, object] | None = None
    for entry in stage_parent.iterdir():
        if not entry.name.startswith("capture-"):
            continue
        identifier = entry.name.removeprefix("capture-")
        try:
            valid_identifier = str(uuid.UUID(identifier)) == identifier
        except ValueError:
            valid_identifier = False
        if not valid_identifier:
            continue
        try:
            request_payload = _read_bounded_private_file(
                entry / CAPTURE_REQUEST_DIGEST_FILE,
                maximum_bytes=65,
            )
        except CaptureServiceError:
            # An unrelated quarantined runtime entry must not disable capture.
            # A valid matching sidecar still triggers full artifact validation.
            continue
        try:
            staged_request_digest = request_payload.decode("ascii").removesuffix("\n")
        except UnicodeDecodeError:
            continue
        if (
            len(staged_request_digest) != 64
            or any(
                character not in "0123456789abcdef"
                for character in staged_request_digest
            )
            or request_payload != f"{staged_request_digest}\n".encode("ascii")
        ):
            continue
        if staged_request_digest != request_digest:
            continue
        artifact = _load_existing_capture_artifact(
            entry,
            session_id=entry.name,
            request_digest=request_digest,
        )
        if match is not None:
            raise CaptureServiceError(
                "multiple capture preparations claim the same request"
            )
        match = artifact
    return match


def _read_regular_source(path: Path) -> bytes:
    if path.is_symlink():
        raise CaptureServiceError("overview must be a readable regular non-symlink file")
    flags = (
        os.O_RDONLY
        | getattr(os, "O_CLOEXEC", 0)
        | getattr(os, "O_NOFOLLOW", 0)
        | getattr(os, "O_NONBLOCK", 0)
    )
    try:
        descriptor = os.open(path, flags)
    except OSError as error:
        raise CaptureServiceError(
            "overview must be a readable regular non-symlink file"
        ) from error
    try:
        handle = os.fdopen(descriptor, "rb", closefd=True)
    except BaseException:
        try:
            os.close(descriptor)
        except OSError:
            pass
        raise
    with handle:
        metadata = os.fstat(handle.fileno())
        if not stat.S_ISREG(metadata.st_mode):
            raise CaptureServiceError("overview must be a regular non-symlink file")
        if metadata.st_size > MAX_CAPTURE_SOURCE_BYTES:
            raise CaptureServiceError("overview exceeds the capture byte limit")
        data = handle.read(MAX_CAPTURE_SOURCE_BYTES + 1)
    if not data:
        raise CaptureServiceError("overview image is empty")
    if len(data) > MAX_CAPTURE_SOURCE_BYTES:
        raise CaptureServiceError("overview exceeds the capture byte limit")
    return data


def _image_dimensions(data: bytes) -> tuple[int, int, str]:
    try:
        from PIL import Image, ImageOps
    except ImportError as error:  # pragma: no cover - dependency declaration proves normal path.
        raise CaptureServiceError("capture requires Pillow") from error
    try:
        from io import BytesIO

        with Image.open(BytesIO(data)) as image:
            content_type = image.get_format_mimetype()
            width, height = image.size
            if width * height > MAX_CAPTURE_PIXELS:
                raise CaptureServiceError("overview exceeds the capture pixel limit")
            image.load()
            oriented = ImageOps.exif_transpose(image)
            try:
                width, height = oriented.size
            finally:
                if oriented is not image:
                    oriented.close()
    except CaptureServiceError:
        raise
    except (Image.DecompressionBombError, OSError, ValueError) as error:
        raise CaptureServiceError("overview is not a valid decodable image") from error
    if type(width) is not int or type(height) is not int or width < 1 or height < 1:
        raise CaptureServiceError("overview image has invalid dimensions")
    if not isinstance(content_type, str) or not content_type.startswith("image/"):
        raise CaptureServiceError("overview image format has no supported media type")
    return width, height, content_type


def parse_segments(
    value: object, *, allow_empty: bool = False
) -> tuple[CaptureSegment, ...]:
    if not isinstance(value, list) or (not value and not allow_empty):
        raise CaptureServiceError("segments must be a non-empty JSON array")
    if len(value) > MAX_CAPTURE_SEGMENTS:
        raise CaptureServiceError("segments exceed the capture segment limit")
    try:
        segments = tuple(
            CaptureSegment(
                segment_id=row["segment_id"],
                region=ImageRegion.from_mapping(row["region"], f"segments[{index}].region"),
            )
            for index, row in enumerate(value)
            if isinstance(row, dict) and set(row) == {"segment_id", "region"}
        )
    except (CaptureError, KeyError, TypeError) as error:
        raise CaptureServiceError(f"segments are malformed: {error}") from error
    if len(segments) != len(value):
        raise CaptureServiceError("each segment must contain exactly segment_id and region")
    if len({segment.segment_id for segment in segments}) != len(segments):
        raise CaptureServiceError("segment_id values must be unique")
    return segments


def _adapter_analysis(
    adapter_name: str | None,
    *,
    registry: AdapterRegistry | None,
    source: dict[str, object],
    source_data: bytes,
    segments: tuple[CaptureSegment, ...],
    timeout_seconds: float,
) -> AdapterResponse | None:
    if adapter_name is None:
        return None
    if registry is None:
        raise CaptureServiceError("adapter name requires a configured server-owned registry")
    return run_local_adapter(
        adapter_name=adapter_name,
        registry=registry,
        request={"protocol_version": 1, "source": source, "segments": [segment.to_dict() for segment in segments]},
        source_data=source_data,
        timeout_seconds=timeout_seconds,
    )


def _identifier_strings(value: object) -> tuple[str, ...]:
    """Extract literal strings from canonical model identifiers without coercion or inference."""
    if isinstance(value, str):
        return (value,) if value.strip() and value.strip().casefold() != "unknown" else ()
    if isinstance(value, Mapping):
        return tuple(part for child in value.values() for part in _identifier_strings(child))
    if isinstance(value, list):
        return tuple(part for child in value for part in _identifier_strings(child))
    return ()


_BARCODE_IDENTIFIER_KEYS = {
    "barcode",
    "ean",
    "ean8",
    "ean13",
    "gtin",
    "gtin8",
    "gtin12",
    "gtin13",
    "gtin14",
    "isbn",
    "isbn10",
    "isbn13",
    "upc",
    "upca",
    "upce",
}


def _barcode_identifier_strings(value: object) -> tuple[str, ...]:
    """Extract only values explicitly labelled as standard barcode identifiers."""
    if isinstance(value, list):
        return tuple(
            part for child in value for part in _barcode_identifier_strings(child)
        )
    if not isinstance(value, Mapping):
        return ()
    result: list[str] = []
    for key, child in value.items():
        normalized_key = "".join(character for character in str(key).casefold() if character.isalnum())
        if normalized_key in _BARCODE_IDENTIFIER_KEYS:
            result.extend(_identifier_strings(child))
        elif isinstance(child, (Mapping, list)):
            result.extend(_barcode_identifier_strings(child))
    return tuple(result)


def _known_capture_signal(value: object) -> tuple[str, ...]:
    if (
        isinstance(value, str)
        and value.strip()
        and value.strip().casefold() != "unknown"
    ):
        return (value,)
    return ()


def _item_subjects(store: Any, visible: callable) -> tuple[DuplicateSubject, ...]:
    aliases: dict[str, list[str]] = {}
    for row in store.rows["aliases"]:
        if visible(row.get("sensitivity")):
            aliases.setdefault(row["item_id"], []).append(row["alias"])
    result: list[DuplicateSubject] = []
    for item in store.rows["items"]:
        if not visible(item.get("sensitivity")):
            continue
        model = store.get("models", item["model_id"])
        identifiers = model.get("identifiers_json") or "{}"
        try:
            values = json.loads(identifiers)
        except (TypeError, json.JSONDecodeError):
            values = {}
        result.append(
            DuplicateSubject(
                identifier=item["item_id"],
                serials=_known_capture_signal(item.get("serial_or_lot")),
                barcodes=_barcode_identifier_strings(values),
                model_identifiers=_identifier_strings(values),
                aliases=tuple(sorted(set(aliases.get(item["item_id"], [])))),
                display_text=" ".join(
                    value
                    for value in (item.get("name"), model.get("name"), model.get("brand"), model.get("model"))
                    if isinstance(value, str)
                ),
            )
        )
    return tuple(result)


def _observation_subject(observation_id: str, observation: dict[str, object]) -> DuplicateSubject:
    payload = observation.get("payload") if isinstance(observation.get("payload"), dict) else {}
    text = payload.get("text") if observation.get("type") == "ocr" else ""
    serial = payload.get("serial") if observation.get("type") == "ocr" else ""
    barcode = payload.get("value") if observation.get("type") == "barcode" else ""
    text = text if isinstance(text, str) else ""
    barcode = barcode if isinstance(barcode, str) else ""
    return DuplicateSubject(
        identifier=observation_id,
        serials=_known_capture_signal(serial),
        barcodes=_known_capture_signal(barcode),
        model_identifiers=_known_capture_signal(payload.get("model_identifier")),
        aliases=_known_capture_signal(text),
        display_text=text or barcode,
    )


def prepare_capture(
    *, runtime_dir: Path, overview: Path, captured_on: str, segments_value: object,
    evidence_id: str | None, source_ref: str | None, evidence_type: str, sensitivity: str,
    adapter_name: str | None, adapter_registry: AdapterRegistry | None,
    timeout_seconds: float, store: Any, visible: callable, base_digest: str,
    links: object = None,
) -> dict[str, object]:
    if (evidence_id is None) == (source_ref is None):
        raise CaptureServiceError("provide exactly one of evidence_id or source_ref")
    if evidence_type not in {"user_source", "physical_check", "research", "vault_note"}:
        raise CaptureServiceError("capture evidence_type is unsupported")
    if evidence_type == "physical_check":
        raise CaptureServiceError(
            "passive capture cannot assert physical_check; seal a crop-bound physical review instead"
        )
    if sensitivity not in {"low", "personal", "high"}:
        raise CaptureServiceError("capture sensitivity is invalid")
    if (
        not isinstance(base_digest, str)
        or len(base_digest) != 64
        or any(character not in "0123456789abcdef" for character in base_digest)
    ):
        raise CaptureServiceError("capture base_digest is invalid")
    source_path = Path(os.path.abspath(overview.expanduser()))
    data = _read_regular_source(source_path)
    width, height, content_type = _image_dimensions(data)
    segments = parse_segments(
        segments_value,
        allow_empty=adapter_name is not None,
    )
    source = make_source_manifest(source_id=f"source-{_digest(data)[:24]}", data=data, content_type=content_type, image_width=width, image_height=height)
    for segment in segments:
        segment.region.validate_within(width, height)
    if links is None:
        links = {}
    if not isinstance(links, dict) or any(
        not isinstance(key, str) or not isinstance(value, str)
        for key, value in links.items()
    ):
        raise CaptureServiceError(
            "links must be an object mapping observation IDs to visible item IDs"
        )
    if links:
        raise CaptureServiceError(
            "capture preparation cannot pre-link observations; use capture review"
        )
    adapter_identity = None
    if adapter_name is not None:
        if adapter_registry is None:
            raise CaptureServiceError(
                "adapter name requires a configured server-owned registry"
            )
        adapter_identity = adapter_registry.identity_for(adapter_name)
    request_binding = {
        "adapter": adapter_identity,
        "base_digest": base_digest,
        "captured_on": captured_on,
        "evidence_id": evidence_id,
        "evidence_type": evidence_type,
        "links": links,
        "original_name": source_path.name,
        "sensitivity": sensitivity,
        "source": source.to_dict(),
        "supplied_segments": [segment.to_dict() for segment in segments],
        "source_ref": source_ref,
    }
    request_digest = _digest(_canonical(request_binding).encode("utf-8"))
    if runtime_dir.is_symlink() or not runtime_dir.is_dir():
        raise CaptureServiceError("capture runtime must be a real directory")
    stage_parent = runtime_dir / "capture-staging"
    if stage_parent.is_symlink():
        raise CaptureServiceError("capture staging parent must not be a symlink")
    if stage_parent.exists():
        if not stage_parent.is_dir():
            raise CaptureServiceError("capture staging parent must be a real directory")
        _cleanup_abandoned_capture_builds(stage_parent)
        existing_artifact = _find_existing_capture_for_request(
            stage_parent,
            request_digest=request_digest,
        )
        if existing_artifact is not None:
            return existing_artifact
    adapter_response = _adapter_analysis(
        adapter_name,
        registry=adapter_registry,
        source=source.to_dict(),
        source_data=data,
        segments=segments,
        timeout_seconds=timeout_seconds,
    )
    segmentation_source = "supplied"
    if not segments:
        if adapter_response is None or not adapter_response.predicted_segments:
            raise CaptureServiceError(
                "empty segments require an adapter that returns predicted segments"
            )
        segments = adapter_response.predicted_segments
        segmentation_source = "adapter"
    total_crop_pixels = sum(
        segment.region.width * segment.region.height for segment in segments
    )
    if total_crop_pixels > 4 * width * height:
        raise CaptureServiceError("segments exceed the total crop pixel limit")
    observations = (
        [observation.to_dict() for observation in adapter_response.observations]
        if adapter_response is not None
        else []
    )
    item_subjects = _item_subjects(store, visible)
    rankings: dict[str, list[dict[str, object]]] = {}
    ranking_summaries: dict[str, dict[str, object]] = {}
    for index, observation in enumerate(observations, start=1):
        observation_id = f"observation-{index}"
        ranking = rank_duplicate_candidates(
            observation=_observation_subject(observation_id, observation),
            candidates=item_subjects,
            limit=MAX_DUPLICATE_CANDIDATES_PER_OBSERVATION,
        )
        rankings[observation_id] = [
            {
                "item_id": candidate.candidate_id,
                "score": candidate.score,
                "evidence": [entry.__dict__ for entry in candidate.evidence],
            }
            for candidate in ranking.candidates
        ]
        ranking_summaries[observation_id] = {
            "match_count": ranking.match_count,
            "returned_count": len(ranking.candidates),
            "truncated": ranking.match_count > len(ranking.candidates),
        }
    visible_ids = {subject.identifier for subject in item_subjects}
    known_observation_ids = set(rankings)
    if not set(links).issubset(known_observation_ids) or any(item_id not in visible_ids for item_id in links.values()):
        raise CaptureServiceError("links may name only prepared observations and scope-visible items")
    referenced_candidate_ids = {
        candidate["item_id"]
        for candidates in rankings.values()
        for candidate in candidates
    }
    item_sensitivity = {
        row["item_id"]: row["sensitivity"]
        for row in store.rows["items"]
        if row.get("item_id") in referenced_candidate_ids
    }
    sensitivity_order = {"low": 0, "personal": 1, "high": 2}
    sensitivity = max(
        [sensitivity, *item_sensitivity.values()],
        key=sensitivity_order.__getitem__,
    )
    generated_crops: list[tuple[str, bytes, dict[str, object]]] = []
    total_crop_bytes = 0
    for segment in segments:
        crop = generate_crop(source=source, source_data=data, segment=segment)
        total_crop_bytes += crop.manifest.byte_length
        if total_crop_bytes > MAX_CAPTURE_TOTAL_CROP_BYTES:
            raise CaptureServiceError(
                "generated crops exceed the total capture byte limit"
            )
        name = f"{crop.manifest.crop_id}.png"
        generated_crops.append(
            (name, crop.data, {**crop.manifest.to_dict(), "file": name})
        )
    artifact_body = {
        "adapter": adapter_identity,
        "captured_on": captured_on,
        "base_digest": base_digest,
        "evidence_id": evidence_id,
        "source_ref": source_ref,
        "evidence_type": evidence_type,
        "sensitivity": sensitivity,
        "source": {
            **source.to_dict(),
            "file": "overview",
            "original_name": source_path.name,
        },
        "crops": [manifest for _, _, manifest in generated_crops],
        "observations": observations,
        "duplicate_candidates": rankings,
        "duplicate_candidate_summaries": ranking_summaries,
        "segments": [segment.to_dict() for segment in segments],
        "segmentation_source": segmentation_source,
        "request_digest": request_digest,
        "links": links,
    }
    artifact = {
        "format": 1,
        "capture_session_id": "capture-pending",
        **artifact_body,
    }
    session_id = capture_session_id_for_artifact(artifact)
    artifact["capture_session_id"] = session_id
    artifact_bytes = _canonical(artifact).encode("utf-8")
    expected_files = {
        CAPTURE_REQUEST_DIGEST_FILE: f"{request_digest}\n".encode("ascii"),
        "overview": data,
        **{name: crop_data for name, crop_data, _ in generated_crops},
        "artifact.json": artifact_bytes,
    }
    if stage_parent.is_symlink():
        raise CaptureServiceError("capture staging parent must not be a symlink")
    stage_parent.mkdir(exist_ok=True)
    if stage_parent.is_symlink() or not stage_parent.is_dir():
        raise CaptureServiceError("capture staging parent must be a real directory")
    stage_root = stage_parent / session_id
    if stage_root.exists() or stage_root.is_symlink():
        _validate_existing_preparation(stage_root, expected_files)
        return artifact
    build_root = stage_parent / f"{_CAPTURE_BUILD_PREFIX}{uuid.uuid4()}"
    build_root.mkdir(mode=0o700)
    published = False
    try:
        for index, (name, payload) in enumerate(expected_files.items()):
            _write_private_file(build_root / name, payload)
            if (
                index == 0
                and os.environ.get("PROPERTY_INVENTORY_FAIL_CAPTURE_PREPARE")
                == "after-first-file"
            ):
                os._exit(95)
        _fsync_directory(build_root)
        try:
            os.rename(build_root, stage_root)
        except OSError:
            if stage_root.exists() or stage_root.is_symlink():
                _validate_existing_preparation(stage_root, expected_files)
                return artifact
            raise
        published = True
        _fsync_directory(stage_parent)
        if (
            os.environ.get("PROPERTY_INVENTORY_FAIL_CAPTURE_PREPARE")
            == "after-publish"
        ):
            os._exit(94)
    finally:
        if not published and build_root.exists() and not build_root.is_symlink():
            shutil.rmtree(build_root)
            _fsync_directory(stage_parent)
    return artifact


_SYNTHETIC_FORBIDDEN_OUTPUT_FIELDS = frozenset(
    {
        "segments",
        "predicted_segments",
        "observed_fields",
        "expected_barcode",
        "observed_barcode",
        "expected_duplicate_id",
        "ranked_duplicate_ids",
    }
)


def _fixture_index(value: object, field: str, upper_bound: int) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or not 0 <= value < upper_bound:
        raise CaptureServiceError(f"{field} must be an observation index within the adapter output")
    return value


def _fixture_duplicate_subject(value: object, field: str) -> DuplicateSubject:
    if not isinstance(value, Mapping):
        raise CaptureServiceError(f"{field} must be an object")
    allowed = {
        "identifier",
        "serials",
        "barcodes",
        "model_identifiers",
        "aliases",
        "display_text",
        "perceptual_hash",
    }
    if set(value) - allowed or "identifier" not in value:
        raise CaptureServiceError(f"{field} has unsupported duplicate-subject fields")
    tuple_fields: dict[str, tuple[str, ...]] = {}
    for name in ("serials", "barcodes", "model_identifiers", "aliases"):
        raw = value.get(name, [])
        if not isinstance(raw, list) or any(not isinstance(part, str) for part in raw):
            raise CaptureServiceError(f"{field}.{name} must be a string list")
        tuple_fields[name] = tuple(raw)
    try:
        return DuplicateSubject(
            identifier=value["identifier"],
            serials=tuple_fields["serials"],
            barcodes=tuple_fields["barcodes"],
            model_identifiers=tuple_fields["model_identifiers"],
            aliases=tuple_fields["aliases"],
            display_text=value.get("display_text", ""),
            perceptual_hash=value.get("perceptual_hash"),
        )
    except (DuplicateError, TypeError) as error:
        raise CaptureServiceError(f"{field} is not a valid duplicate subject") from error


def _fixture_source(value: object, field: str) -> tuple[bytes, SourceManifest]:
    if not isinstance(value, Mapping) or set(value) != {
        "source_id",
        "base64",
        "content_type",
        "image_width",
        "image_height",
    }:
        raise CaptureServiceError(f"{field} must contain checked source bytes and manifest dimensions")
    encoded = value["base64"]
    if not isinstance(encoded, str):
        raise CaptureServiceError(f"{field}.base64 must be a string")
    try:
        data = base64.b64decode(encoded, validate=True)
    except (ValueError, TypeError) as error:
        raise CaptureServiceError(f"{field}.base64 is malformed") from error
    try:
        source = make_source_manifest(
            source_id=value["source_id"],
            data=data,
            content_type=value["content_type"],
            image_width=value["image_width"],
            image_height=value["image_height"],
        )
    except CaptureError as error:
        raise CaptureServiceError(f"{field} manifest is malformed") from error
    return data, source


def run_synthetic_capture_benchmark(
    *,
    cases: object,
    registry: AdapterRegistry,
    adapter_name: str,
    top_k: int = 3,
    timeout_seconds: float = 10.0,
) -> CaptureBenchmarkReport:
    """Execute checked synthetic fixtures through crops, a named adapter, and duplicate ranking.

    The resulting report deliberately keeps the ``synthetic-fixture-only``
    claim.  Fixture authors provide source bytes, ground truth, and canonical
    candidates, never caller-supplied pipeline predictions.
    """
    if not isinstance(cases, list) or not cases:
        raise CaptureServiceError("synthetic benchmark cases must be a non-empty array")
    if not isinstance(registry, AdapterRegistry):
        raise CaptureServiceError("synthetic benchmark registry is invalid")
    executed_cases: list[dict[str, object]] = []
    for index, case in enumerate(cases):
        if not isinstance(case, Mapping):
            raise CaptureServiceError(f"synthetic benchmark case {index} must be an object")
        forbidden = set(case) & _SYNTHETIC_FORBIDDEN_OUTPUT_FIELDS
        if forbidden:
            raise CaptureServiceError(
                "synthetic benchmark cases must not provide pipeline outputs: "
                + ", ".join(sorted(forbidden))
            )
        case_id = case.get("case_id")
        if not isinstance(case_id, str) or not case_id.strip():
            raise CaptureServiceError(f"synthetic benchmark case {index}.case_id must be non-empty")
        data, source = _fixture_source(case.get("source"), f"{case_id}.source")
        try:
            truth_regions = [
                ImageRegion.from_mapping(
                    region, f"{case_id}.truth_segments[{truth_index}]"
                )
                for truth_index, region in enumerate(case.get("truth_segments", []))
                if isinstance(region, Mapping)
            ]
            for region in truth_regions:
                region.validate_within(source.image_width, source.image_height)
            truth_segments = [region.to_dict() for region in truth_regions]
        except CaptureError as error:
            raise CaptureServiceError(f"{case_id}.truth_segments are malformed") from error
        if not isinstance(case.get("truth_segments"), list) or len(truth_segments) != len(
            case["truth_segments"]
        ):
            raise CaptureServiceError(f"{case_id}.truth_segments must be a list of regions")
        response = run_local_adapter(
            adapter_name=adapter_name,
            registry=registry,
            request={
                "protocol_version": 1,
                "source": source.to_dict(),
                "segments": [],
            },
            source_data=data,
            timeout_seconds=timeout_seconds,
        )
        if not response.predicted_segments:
            raise CaptureServiceError(
                f"{case_id} adapter must return predicted segments"
            )
        try:
            for segment in response.predicted_segments:
                generate_crop(source=source, source_data=data, segment=segment)
        except CaptureError as error:
            raise CaptureServiceError(f"{case_id} crop generation failed") from error
        observations = [observation.to_dict() for observation in response.observations]
        expected_fields = case.get("expected_fields")
        fields = case.get("field_observations")
        if not isinstance(expected_fields, Mapping) or not isinstance(fields, Mapping):
            raise CaptureServiceError(f"{case_id} must contain expected_fields and field_observations")
        if set(expected_fields) != set(fields) or any(not isinstance(name, str) for name in fields):
            raise CaptureServiceError(f"{case_id} field observations must exactly cover expected fields")
        observed_fields: dict[str, object] = {}
        for name, selector in fields.items():
            if not isinstance(selector, Mapping) or set(selector) != {"observation_index", "payload_key"}:
                raise CaptureServiceError(f"{case_id}.field_observations.{name} is malformed")
            payload_key = selector["payload_key"]
            if not isinstance(payload_key, str):
                raise CaptureServiceError(f"{case_id}.field_observations.{name}.payload_key must be a string")
            observation_index = _fixture_index(
                selector["observation_index"], f"{case_id}.field_observations.{name}", len(observations)
            )
            observed_fields[name] = observations[observation_index]["payload"].get(payload_key)
        barcode = case.get("barcode")
        if not isinstance(barcode, Mapping) or set(barcode) != {"expected", "observation_index"}:
            raise CaptureServiceError(f"{case_id}.barcode must contain expected and observation_index")
        barcode_index = _fixture_index(barcode["observation_index"], f"{case_id}.barcode", len(observations))
        observed_barcode = observations[barcode_index]["payload"].get("value")
        duplicate = case.get("duplicate")
        if not isinstance(duplicate, Mapping) or set(duplicate) != {
            "expected_id",
            "observation_index",
            "candidates",
        }:
            raise CaptureServiceError(f"{case_id}.duplicate must contain expected_id, observation_index, and candidates")
        duplicate_index = _fixture_index(
            duplicate["observation_index"], f"{case_id}.duplicate", len(observations)
        )
        candidates_raw = duplicate["candidates"]
        if not isinstance(candidates_raw, list):
            raise CaptureServiceError(f"{case_id}.duplicate.candidates must be a list")
        candidates = tuple(
            _fixture_duplicate_subject(candidate, f"{case_id}.duplicate.candidates[{candidate_index}]")
            for candidate_index, candidate in enumerate(candidates_raw)
        )
        ranking = rank_duplicate_candidates(
            observation=_observation_subject(f"{case_id}-observation", observations[duplicate_index]),
            candidates=candidates,
        )
        executed_cases.append(
            {
                "case_id": case_id,
                "truth_segments": truth_segments,
                "predicted_segments": [
                    segment.region.to_dict() for segment in response.predicted_segments
                ],
                "expected_fields": dict(expected_fields),
                "observed_fields": observed_fields,
                "expected_barcode": barcode["expected"],
                "observed_barcode": observed_barcode,
                "expected_duplicate_id": duplicate["expected_id"],
                "ranked_duplicate_ids": [candidate.candidate_id for candidate in ranking.candidates],
            }
        )
    return run_capture_benchmark(cases=executed_cases, corpus_label="synthetic", top_k=top_k)
