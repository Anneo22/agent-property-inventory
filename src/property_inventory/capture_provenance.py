"""Pure validation for durable capture artifacts and sealed reviews."""

from __future__ import annotations

import hashlib
import json
import math
import re
import uuid
from collections.abc import Mapping
from datetime import date, datetime

try:
    from .capture import (
        MAX_CAPTURE_CROP_BYTES,
        MAX_CAPTURE_SEGMENTS,
        MAX_CAPTURE_TOTAL_CROP_BYTES,
        CaptureError,
        CaptureSegment,
        CropManifest,
        ImageRegion,
        SourceManifest,
        normalize_observations,
        validate_json_value,
    )
    from .json_codec import StrictJSONError
    from .json_codec import loads as strict_json_loads
except ImportError:  # direct script execution by the CLI
    from capture import (
        MAX_CAPTURE_CROP_BYTES,
        MAX_CAPTURE_SEGMENTS,
        MAX_CAPTURE_TOTAL_CROP_BYTES,
        CaptureError,
        CaptureSegment,
        CropManifest,
        ImageRegion,
        SourceManifest,
        normalize_observations,
        validate_json_value,
    )
    from json_codec import StrictJSONError
    from json_codec import loads as strict_json_loads


class CaptureProvenanceError(ValueError):
    """Raised when durable capture provenance is incomplete or inconsistent."""


ARTIFACT_KEYS = frozenset(
    {
        "adapter",
        "base_digest",
        "capture_session_id",
        "captured_on",
        "crops",
        "duplicate_candidate_summaries",
        "duplicate_candidates",
        "evidence_id",
        "evidence_type",
        "format",
        "links",
        "observations",
        "request_digest",
        "segmentation_source",
        "segments",
        "sensitivity",
        "source",
        "source_ref",
    }
)
REVIEW_KEYS = frozenset(
    {
        "artifact_sha256",
        "base_digest",
        "capture_session_id",
        "created_at",
        "format",
        "links",
        "manual_observations",
        "decisions",
        "proposal_id",
    }
)
EVIDENCE_KINDS = frozenset(
    {
        "exact_alias",
        "exact_barcode",
        "exact_model_identifier",
        "exact_serial",
        "perceptual_hash_candidate",
        "token_overlap",
    }
)
SENSITIVITIES = frozenset({"low", "personal", "high"})
EVIDENCE_TYPES = frozenset(
    {"user_source", "physical_check", "research", "vault_note"}
)
_CAPTURE_SESSION_NAMESPACE = uuid.UUID("96248de2-a4cf-4d60-badd-3e0bd9182df9")


def _sha256(value: object) -> bool:
    return isinstance(value, str) and re.fullmatch(r"[0-9a-f]{64}", value) is not None


def decode_json_bytes(payload: bytes, label: str) -> object:
    """Decode finite UTF-8 JSON while rejecting duplicate object keys."""
    try:
        return strict_json_loads(payload, label=label)
    except StrictJSONError as error:
        raise CaptureProvenanceError(f"{label} is not strict UTF-8 JSON") from error


def canonical_artifact_bytes(artifact: Mapping[str, object]) -> bytes:
    try:
        return json.dumps(
            artifact,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        ).encode("utf-8")
    except (RecursionError, TypeError, UnicodeEncodeError, ValueError) as error:
        raise CaptureProvenanceError("capture artifact is not canonical JSON") from error


def canonical_review_bytes(review: Mapping[str, object]) -> bytes:
    try:
        return (
            json.dumps(
                review,
                ensure_ascii=False,
                indent=2,
                sort_keys=True,
                allow_nan=False,
            )
            + "\n"
        ).encode("utf-8")
    except (RecursionError, TypeError, UnicodeEncodeError, ValueError) as error:
        raise CaptureProvenanceError("capture review is not canonical JSON") from error


def capture_session_id_for_artifact(artifact: Mapping[str, object]) -> str:
    """Return the content-addressed session ID for one complete artifact."""
    if (
        not isinstance(artifact, Mapping)
        or type(artifact.get("format")) is not int
        or artifact.get("format") != 1
        or not isinstance(artifact.get("request_digest"), str)
    ):
        raise CaptureProvenanceError("capture artifact cannot derive a session ID")
    body = {
        key: value
        for key, value in artifact.items()
        if key not in {"format", "capture_session_id"}
    }
    digest = hashlib.sha256(canonical_artifact_bytes(body)).hexdigest()
    return f"capture-{uuid.uuid5(_CAPTURE_SESSION_NAMESPACE, digest)}"


def _validate_candidates(artifact: Mapping[str, object], observation_ids: set[str]) -> None:
    candidates = artifact.get("duplicate_candidates")
    summaries = artifact.get("duplicate_candidate_summaries")
    if (
        not isinstance(candidates, dict)
        or set(candidates) != observation_ids
        or not isinstance(summaries, dict)
        or set(summaries) != observation_ids
    ):
        raise CaptureProvenanceError("capture artifact candidate ranking is malformed")
    for observation_id in sorted(observation_ids):
        ranking = candidates[observation_id]
        summary = summaries[observation_id]
        if (
            not isinstance(ranking, list)
            or len(ranking) > 5
            or not isinstance(summary, dict)
            or set(summary) != {"match_count", "returned_count", "truncated"}
        ):
            raise CaptureProvenanceError("capture artifact candidate ranking is malformed")
        candidate_ids: set[str] = set()
        ordering: list[tuple[float, str]] = []
        for candidate in ranking:
            if (
                not isinstance(candidate, dict)
                or set(candidate) != {"evidence", "item_id", "score"}
                or not isinstance(candidate.get("item_id"), str)
                or not candidate["item_id"].strip()
                or candidate["item_id"] in candidate_ids
                or isinstance(candidate.get("score"), bool)
                or not isinstance(candidate.get("score"), (int, float))
                or not math.isfinite(float(candidate["score"]))
                or float(candidate["score"]) <= 0
                or not isinstance(candidate.get("evidence"), list)
                or not candidate["evidence"]
            ):
                raise CaptureProvenanceError(
                    "capture artifact candidate ranking is malformed"
                )
            candidate_ids.add(candidate["item_id"])
            ordering.append((-float(candidate["score"]), candidate["item_id"]))
            evidence_total = 0.0
            for evidence in candidate["evidence"]:
                if (
                    not isinstance(evidence, dict)
                    or set(evidence) != {"detail", "kind", "score"}
                    or evidence.get("kind") not in EVIDENCE_KINDS
                    or not isinstance(evidence.get("detail"), str)
                    or not evidence["detail"].strip()
                    or isinstance(evidence.get("score"), bool)
                    or not isinstance(evidence.get("score"), (int, float))
                    or not math.isfinite(float(evidence["score"]))
                    or float(evidence["score"]) <= 0
                ):
                    raise CaptureProvenanceError(
                        "capture artifact candidate evidence is malformed"
                    )
                evidence_total += float(evidence["score"])
            if not math.isclose(
                evidence_total,
                float(candidate["score"]),
                rel_tol=1e-12,
                abs_tol=1e-12,
            ):
                raise CaptureProvenanceError(
                    "capture artifact candidate score is malformed"
                )
        match_count = summary.get("match_count")
        returned_count = summary.get("returned_count")
        truncated = summary.get("truncated")
        if (
            ordering != sorted(ordering)
            or isinstance(match_count, bool)
            or not isinstance(match_count, int)
            or match_count < len(ranking)
            or isinstance(returned_count, bool)
            or not isinstance(returned_count, int)
            or returned_count != len(ranking)
            or not isinstance(truncated, bool)
            or truncated != (match_count > len(ranking))
        ):
            raise CaptureProvenanceError(
                "capture artifact candidate summary is malformed"
            )


def validate_capture_artifact(
    value: object, *, expected_session_id: str | None = None
) -> dict[str, object]:
    """Validate the complete immutable preparation artifact."""
    try:
        validate_json_value(value, "capture artifact", max_depth=64)
    except CaptureError as error:
        raise CaptureProvenanceError(str(error)) from error
    if not isinstance(value, dict) or set(value) != ARTIFACT_KEYS:
        raise CaptureProvenanceError("capture artifact is malformed")
    artifact = value
    if (
        type(artifact.get("format")) is not int
        or artifact.get("format") != 1
        or not isinstance(artifact.get("capture_session_id"), str)
        or not re.fullmatch(r"capture-[0-9a-f-]{36}", artifact["capture_session_id"])
        or (expected_session_id is not None and artifact["capture_session_id"] != expected_session_id)
        or not _sha256(artifact.get("base_digest"))
        or not _sha256(artifact.get("request_digest"))
        or artifact.get("sensitivity") not in SENSITIVITIES
        or artifact.get("evidence_type") not in EVIDENCE_TYPES
        or (artifact.get("evidence_id") is None) == (artifact.get("source_ref") is None)
        or (
            artifact.get("evidence_id") is not None
            and (
                not isinstance(artifact["evidence_id"], str)
                or not artifact["evidence_id"].strip()
            )
        )
        or (
            artifact.get("source_ref") is not None
            and (
                not isinstance(artifact["source_ref"], str)
                or not artifact["source_ref"].strip()
            )
        )
        or (
            artifact.get("source_ref") is not None
            and artifact.get("evidence_type") == "physical_check"
        )
    ):
        raise CaptureProvenanceError("capture artifact is malformed")
    adapter = artifact.get("adapter")
    if adapter is not None:
        revision = adapter.get("revision") if isinstance(adapter, dict) else None
        try:
            revision_bytes = revision.encode("utf-8") if isinstance(revision, str) else b""
        except UnicodeEncodeError as error:
            raise CaptureProvenanceError(
                "capture artifact adapter identity is malformed"
            ) from error
        if (
            not isinstance(adapter, dict)
            or set(adapter) != {"command_sha256", "name", "revision"}
            or not _sha256(adapter.get("command_sha256"))
            or not isinstance(adapter.get("name"), str)
            or not adapter["name"].strip()
            or not isinstance(revision, str)
            or not revision.strip()
            or revision != revision.strip()
            or len(revision_bytes) > 256
            or any(ord(character) < 32 for character in revision)
        ):
            raise CaptureProvenanceError(
                "capture artifact adapter identity is malformed"
            )
    try:
        captured_on = date.fromisoformat(artifact["captured_on"])
    except (TypeError, ValueError) as error:
        raise CaptureProvenanceError("capture artifact captured_on is malformed") from error
    if captured_on.isoformat() != artifact["captured_on"]:
        raise CaptureProvenanceError("capture artifact captured_on is not canonical")

    source_value = artifact.get("source")
    source_keys = {
        "byte_length",
        "content_type",
        "coordinate_space",
        "file",
        "image_height",
        "image_width",
        "original_name",
        "sha256",
        "source_id",
    }
    if (
        not isinstance(source_value, dict)
        or set(source_value) != source_keys
        or source_value.get("file") != "overview"
        or not isinstance(source_value.get("original_name"), str)
        or not source_value["original_name"].strip()
        or not isinstance(source_value.get("content_type"), str)
        or not source_value["content_type"].startswith("image/")
    ):
        raise CaptureProvenanceError("capture artifact source is malformed")
    try:
        source = SourceManifest(
            source_id=source_value["source_id"],
            sha256=source_value["sha256"],
            byte_length=source_value["byte_length"],
            content_type=source_value["content_type"],
            image_width=source_value["image_width"],
            image_height=source_value["image_height"],
            coordinate_space=source_value["coordinate_space"],
        )
    except (KeyError, CaptureError) as error:
        raise CaptureProvenanceError("capture artifact source is malformed") from error
    if source.source_id != f"source-{source.sha256[:24]}":
        raise CaptureProvenanceError("capture source_id does not bind its digest")

    segments_value = artifact.get("segments")
    crops_value = artifact.get("crops")
    if (
        not isinstance(segments_value, list)
        or not segments_value
        or len(segments_value) > MAX_CAPTURE_SEGMENTS
        or not isinstance(crops_value, list)
        or len(crops_value) != len(segments_value)
        or artifact.get("segmentation_source") not in {"supplied", "adapter"}
        or (artifact.get("segmentation_source") == "adapter" and adapter is None)
    ):
        raise CaptureProvenanceError("capture artifact segments are malformed")
    segments: list[CaptureSegment] = []
    segment_ids: set[str] = set()
    for index, segment_value in enumerate(segments_value):
        try:
            if not isinstance(segment_value, Mapping) or set(segment_value) != {
                "region",
                "segment_id",
            }:
                raise CaptureError("segment has unexpected fields")
            region_value = segment_value["region"]
            if not isinstance(region_value, Mapping):
                raise CaptureError("segment region must be an object")
            segment = CaptureSegment(
                segment_id=segment_value["segment_id"],
                region=ImageRegion.from_mapping(
                    region_value, f"segments[{index}].region"
                ),
            )
            segment.region.validate_within(source.image_width, source.image_height)
        except (KeyError, CaptureError) as error:
            raise CaptureProvenanceError("capture artifact segment is malformed") from error
        if segment.segment_id in segment_ids:
            raise CaptureProvenanceError("capture segment IDs must be unique")
        segment_ids.add(segment.segment_id)
        segments.append(segment)
    if sum(segment.region.width * segment.region.height for segment in segments) > (
        4 * source.image_width * source.image_height
    ):
        raise CaptureProvenanceError("capture segments exceed the total pixel limit")

    crop_keys = {
        "byte_length",
        "content_type",
        "crop_id",
        "file",
        "region",
        "segment_id",
        "sha256",
        "source_id",
        "source_sha256",
    }
    crop_files: set[str] = set()
    total_crop_bytes = 0
    for crop_value, segment in zip(crops_value, segments, strict=True):
        if not isinstance(crop_value, dict) or set(crop_value) != crop_keys:
            raise CaptureProvenanceError("capture artifact crop is malformed")
        try:
            region_value = crop_value["region"]
            if not isinstance(region_value, Mapping):
                raise CaptureError("crop region must be an object")
            crop = CropManifest(
                crop_id=crop_value["crop_id"],
                source_id=crop_value["source_id"],
                source_sha256=crop_value["source_sha256"],
                segment_id=crop_value["segment_id"],
                region=ImageRegion.from_mapping(region_value, "crop.region"),
                sha256=crop_value["sha256"],
                byte_length=crop_value["byte_length"],
                content_type=crop_value["content_type"],
            )
        except (KeyError, CaptureError) as error:
            raise CaptureProvenanceError("capture artifact crop is malformed") from error
        if (
            crop.source_id != source.source_id
            or crop.source_sha256 != source.sha256
            or crop.segment_id != segment.segment_id
            or crop.region != segment.region
            or crop.byte_length > MAX_CAPTURE_CROP_BYTES
            or crop_value.get("file") != f"{crop.crop_id}.png"
            or crop_value["file"] in crop_files
        ):
            raise CaptureProvenanceError("capture crop disagrees with its source segment")
        crop_files.add(crop_value["file"])
        total_crop_bytes += crop.byte_length
    if total_crop_bytes > MAX_CAPTURE_TOTAL_CROP_BYTES:
        raise CaptureProvenanceError("capture crops exceed the total byte limit")

    observations_value = artifact.get("observations")
    if not isinstance(observations_value, list):
        raise CaptureProvenanceError("capture artifact observations are malformed")
    try:
        observations = normalize_observations(
            observations_value,
            image_width=source.image_width,
            image_height=source.image_height,
        )
    except CaptureError as error:
        raise CaptureProvenanceError("capture artifact observation is malformed") from error
    if [observation.to_dict() for observation in observations] != observations_value:
        raise CaptureProvenanceError("capture artifact observations are not canonical")
    if adapter is None and observations:
        raise CaptureProvenanceError(
            "capture observations require a durable adapter identity"
        )
    observation_ids = {
        f"observation-{index}" for index in range(1, len(observations) + 1)
    }
    links = artifact.get("links")
    if (
        not isinstance(links, dict)
        or links
        or any(
            not isinstance(key, str)
            or key not in observation_ids
            or not isinstance(item_id, str)
            or not item_id.strip()
            for key, item_id in links.items()
        )
    ):
        raise CaptureProvenanceError("capture artifact links are malformed")
    _validate_candidates(artifact, observation_ids)
    try:
        derived_session_id = capture_session_id_for_artifact(artifact)
    except ValueError as error:
        raise CaptureProvenanceError(
            "capture artifact cannot derive its content-addressed session"
        ) from error
    if derived_session_id != artifact["capture_session_id"]:
        raise CaptureProvenanceError(
            "capture artifact disagrees with its content-addressed session"
        )
    return artifact


def _observation_is_within_crop(observation: Mapping[str, object], crop: Mapping[str, object]) -> bool:
    """Require an adapter observation's full source rectangle inside one crop.

    Coordinates use half-open pixels, so equality at a crop edge is permitted.
    This is deliberately stricter than non-zero overlap: an OCR, serial, or
    barcode rectangle crossing into another segment is ambiguous and cannot
    support an identity decision for either crop.
    """
    try:
        observed = ImageRegion.from_mapping(
            observation["region"], "observation.region"
        )
        selected = ImageRegion.from_mapping(crop["region"], "crop.region")
    except (KeyError, CaptureError):
        return False
    return (
        selected.x <= observed.x
        and selected.y <= observed.y
        and observed.right <= selected.right
        and observed.bottom <= selected.bottom
    )


def validate_capture_review(
    value: object,
    *,
    artifact: Mapping[str, object] | None = None,
    expected_session_id: str | None = None,
) -> dict[str, object]:
    """Validate one sealed, digest-bound human review."""
    try:
        validate_json_value(value, "capture review", max_depth=64)
    except CaptureError as error:
        raise CaptureProvenanceError(str(error)) from error
    if not isinstance(value, dict) or set(value) != REVIEW_KEYS:
        raise CaptureProvenanceError("capture review is malformed")
    review = value
    if (
        type(review.get("format")) is not int
        or review.get("format") != 1
        or not isinstance(review.get("capture_session_id"), str)
        or not re.fullmatch(r"capture-[0-9a-f-]{36}", review["capture_session_id"])
        or (expected_session_id is not None and review["capture_session_id"] != expected_session_id)
        or not _sha256(review.get("artifact_sha256"))
        or not _sha256(review.get("base_digest"))
        or not isinstance(review.get("proposal_id"), str)
        or not re.fullmatch(r"proposal-[0-9a-f-]{36}", review["proposal_id"])
        or not isinstance(review.get("links"), dict)
        or any(
            not isinstance(key, str)
            or not key
            or not isinstance(item_id, str)
            or not item_id.strip()
            for key, item_id in review["links"].items()
        )
    ):
        raise CaptureProvenanceError("capture review is malformed")
    try:
        created_at = datetime.fromisoformat(review["created_at"])
    except (TypeError, ValueError) as error:
        raise CaptureProvenanceError("capture review created_at is malformed") from error
    if created_at.tzinfo is None or created_at.isoformat() != review["created_at"]:
        raise CaptureProvenanceError("capture review created_at is not canonical")
    if artifact is not None:
        validated_artifact = validate_capture_artifact(
            dict(artifact), expected_session_id=review["capture_session_id"]
        )
        observation_ids = {
            f"observation-{index}"
            for index in range(1, len(validated_artifact["observations"]) + 1)
        }
        adapter_observations = {
            f"observation-{index}": observation
            for index, observation in enumerate(
                validated_artifact["observations"], start=1
            )
        }
        if (
            review["base_digest"] != validated_artifact["base_digest"]
            or review["artifact_sha256"]
            != hashlib.sha256(canonical_artifact_bytes(validated_artifact)).hexdigest()
            or not set(review["links"]).issubset(observation_ids)
        ):
            raise CaptureProvenanceError(
                "capture review disagrees with its preparation artifact"
            )
        crop_by_id = {
            crop["crop_id"]: crop
            for crop in validated_artifact["crops"]
        }
        segment_by_id = {
            segment["segment_id"]: segment
            for segment in validated_artifact["segments"]
        }
        manual = review.get("manual_observations")
        if not isinstance(manual, dict):
            raise CaptureProvenanceError("capture review manual observations are malformed")
        manual_ids: set[str] = set()
        for manual_id, entry in manual.items():
            if (
                not isinstance(manual_id, str)
                or not re.fullmatch(r"manual-[a-z0-9][a-z0-9-]{0,63}", manual_id)
                or manual_id in manual_ids
                or not isinstance(entry, dict)
                or set(entry) != {"crop_id", "observation", "segment_id"}
                or not isinstance(entry.get("crop_id"), str)
                or not isinstance(entry.get("segment_id"), str)
                or not isinstance(entry.get("observation"), dict)
            ):
                raise CaptureProvenanceError(
                    "capture review manual observations are malformed"
                )
            crop = crop_by_id.get(entry["crop_id"])
            segment = segment_by_id.get(entry["segment_id"])
            if crop is None or segment is None or crop["segment_id"] != entry["segment_id"]:
                raise CaptureProvenanceError(
                    "capture review manual observation crop disagrees with segment"
                )
            try:
                validate_json_value(entry["observation"], "manual observation", max_depth=32)
            except CaptureError as error:
                raise CaptureProvenanceError(str(error)) from error
            manual_ids.add(manual_id)
        decisions = review.get("decisions")
        if not isinstance(decisions, list):
            raise CaptureProvenanceError("capture review decisions are malformed")
        seen_crops: set[str] = set()
        seen_observations: set[str] = set()
        for decision in decisions:
            if (
                not isinstance(decision, dict)
                or set(decision) != {
                    "crop_id",
                    "discovery",
                    "item_id",
                    "observation_id",
                    "physical",
                    "segment_id",
                }
                or not isinstance(decision.get("crop_id"), str)
                or not isinstance(decision.get("segment_id"), str)
                or (
                    decision.get("observation_id") is not None
                    and not isinstance(decision.get("observation_id"), str)
                )
                or (
                    decision.get("item_id") is None
                    and decision.get("discovery") is None
                )
                or (
                    decision.get("item_id") is not None
                    and decision.get("discovery") is not None
                )
                or (
                    decision.get("item_id") is not None
                    and not isinstance(decision.get("item_id"), str)
                )
                or (
                    decision.get("discovery") is not None
                    and not isinstance(decision.get("discovery"), dict)
                )
                or not isinstance(decision.get("physical"), dict)
            ):
                raise CaptureProvenanceError("capture review decisions are malformed")
            crop = crop_by_id.get(decision["crop_id"])
            segment = segment_by_id.get(decision["segment_id"])
            if (
                crop is None
                or segment is None
                or crop["segment_id"] != decision["segment_id"]
                or decision["crop_id"] in seen_crops
            ):
                raise CaptureProvenanceError(
                    "capture review decision crop disagrees with segment"
                )
            observation_id = decision.get("observation_id")
            if observation_id is not None and observation_id not in observation_ids | manual_ids:
                raise CaptureProvenanceError(
                    "capture review decision names an unknown observation"
                )
            if observation_id is not None:
                if observation_id in seen_observations:
                    raise CaptureProvenanceError(
                        "capture review reuses an observation across decisions"
                    )
                seen_observations.add(observation_id)
                linked_item_id = review["links"].get(observation_id)
                if linked_item_id is not None and linked_item_id != decision.get("item_id"):
                    raise CaptureProvenanceError(
                        "capture review decision disagrees with its observation link"
                    )
            if decision["physical"].get("checked_on") != validated_artifact["captured_on"]:
                raise CaptureProvenanceError(
                    "capture review physical check date disagrees with capture date"
                )
            if observation_id in manual_ids:
                manual_observation = manual[observation_id]
                if (
                    manual_observation["crop_id"] != decision["crop_id"]
                    or manual_observation["segment_id"] != decision["segment_id"]
                ):
                    raise CaptureProvenanceError(
                        "capture review decision manual observation disagrees with its crop"
                    )
            elif observation_id is not None and not _observation_is_within_crop(
                adapter_observations[observation_id], crop
            ):
                raise CaptureProvenanceError(
                    "capture review decision adapter observation is not fully contained by its crop"
                )
            seen_crops.add(decision["crop_id"])
    elif review.get("manual_observations") != {} or review.get("decisions") != []:
        raise CaptureProvenanceError("capture review requires its preparation artifact")
    return review
