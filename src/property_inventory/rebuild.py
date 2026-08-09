#!/usr/bin/env python3
"""Rebuild the disposable SQLite query index from canonical JSONL tables."""

from __future__ import annotations

import argparse
import hashlib
import json
import math
import os
import re
import sqlite3
from datetime import date, datetime
from pathlib import Path

try:
    from .capture import CaptureError, normalize_observation
    from .capture_provenance import (
        CaptureProvenanceError,
        canonical_artifact_bytes,
        canonical_review_bytes,
        decode_json_bytes,
        validate_capture_artifact,
        validate_capture_review,
    )
    from .json_codec import StrictJSONError
    from .json_codec import dumps as strict_json_dumps
    from .json_codec import loads as strict_json_loads
    from .spatial import SpatialValidationError, normalize_spatial_profile
    from .valuation_policy import valuation_evidence_supports_basis
except ImportError:  # direct script execution by the CLI
    from capture import CaptureError, normalize_observation
    from capture_provenance import (
        CaptureProvenanceError,
        canonical_artifact_bytes,
        canonical_review_bytes,
        decode_json_bytes,
        validate_capture_artifact,
        validate_capture_review,
    )
    from json_codec import StrictJSONError
    from json_codec import dumps as strict_json_dumps
    from json_codec import loads as strict_json_loads
    from spatial import SpatialValidationError, normalize_spatial_profile
    from valuation_policy import valuation_evidence_supports_basis

SCHEMA_VERSION = 6
TABLES = (
    "metadata",
    "proposal_commits",
    "locations",
    "models",
    "evidence",
    "media_assets",
    "interfaces",
    "items",
    "item_evidence",
    "evidence_assets",
    "model_interfaces",
    "relationships",
    "item_documents",
    "torque_paths",
    "kits",
    "kit_requirements",
    "item_tags",
    "aliases",
    "spatial_profiles",
    "valuations",
    "capture_sessions",
    "capture_observations",
    "maintenance_sessions",
    "maintenance_session_items",
    "sync_receipts",
    "kit_reviews",
    "item_dimensions",
    "item_amendments",
    "item_detail_amendments",
    "fact_amendments",
    "inventory_events",
)


class SchemaVersionError(ValueError):
    """The canonical store cannot be read by this schema version."""


SENSITIVITY_RANK = {"low": 0, "personal": 1, "high": 2}
CONTAINER_LOCATION_KINDS = frozenset({"container", "vehicle", "asset"})
CHRONOLOGICAL_EVENT_TYPES = frozenset(
    {
        "planned",
        "ordered",
        "received",
        "sold",
        "returned",
        "cancelled",
        "refunded",
        "gifted",
        "disposed",
        "lost",
        "lent",
        "loan_returned",
        "ownership_unresolved",
        "ownership_excluded",
        "quantity_changed",
        "moved",
        "physically_verified",
        "not_found_in_area",
        "reacquired",
        "ownership_corrected",
    }
)

EVENT_CLAIM_REQUIREMENTS = {
    "planned": "purchase_only",
    "ordered": "purchase_only",
    "received": "explicit_current",
    "reacquired": "explicit_current",
    "ownership_corrected": "explicit_current",
    "moved": "explicit_current",
    "lent": "explicit_current",
    "loan_returned": "explicit_current",
    "quantity_changed": "explicit_current",
    "sold": "explicit_not_owned",
    "returned": "explicit_not_owned",
    "cancelled": "explicit_not_owned",
    "refunded": "explicit_not_owned",
    "gifted": "explicit_not_owned",
    "disposed": "explicit_not_owned",
    "lost": "explicit_not_owned",
    "ownership_excluded": "explicit_not_owned",
    "ownership_unresolved": "claimed_owned",
    "not_found_in_area": "area_not_found",
    "physically_verified": "explicit_current",
}

EVENT_EVIDENCE_TYPE_REQUIREMENTS = {
    "not_found_in_area": "physical_check",
    "physically_verified": "physical_check",
}

LOCATION_CONTEXT_EVENT_TYPES = frozenset(
    {
        "planned",
        "ordered",
        "received",
        "physically_verified",
        "moved",
        "quantity_changed",
        "lent",
        "loan_returned",
        "ownership_unresolved",
        "not_found_in_area",
        "reacquired",
        "ownership_corrected",
    }
)

STABLE_PHYSICAL_LOCATION_EVENT_TYPES = frozenset(
    {"physically_verified", "reacquired", "ownership_corrected"}
)
ITEM_DETAIL_FIELDS = (
    "acquired_on",
    "condition",
    "purchase_currency",
    "purchase_price",
    "receipt_ref",
    "serial_or_lot",
)
REACQUISITION_RESET_FIELDS = (
    "acquired_on",
    "condition",
    "purchase_currency",
    "purchase_price",
    "receipt_ref",
)
FACT_SELECTOR_FIELDS = {
    "locations": ("location_id",),
    "aliases": ("alias_id",),
    "item_tags": ("item_id", "tag"),
    "relationships": ("relationship_id",),
    "torque_paths": ("path_id",),
    "spatial_profiles": ("profile_id",),
    "kits": ("kit_id",),
    "kit_requirements": ("kit_id", "requirement_key"),
    "model_interfaces": ("model_id", "interface_id", "role"),
    "valuations": ("valuation_id",),
    "item_documents": ("document_id",),
    "item_dimensions": ("dimension_id",),
}
FACT_ROW_FIELDS = {
    "locations": {"location_id", "name", "parent_location_id", "kind", "sensitivity", "notes"},
    "aliases": {
        "alias_id",
        "item_id",
        "alias",
        "alias_kind",
        "evidence_id",
        "sensitivity",
        "notes",
    },
    "item_tags": {"item_id", "tag", "evidence_id", "sensitivity", "notes"},
    "relationships": {
        "relationship_id",
        "subject_item_id",
        "predicate",
        "object_item_id",
        "confidence",
        "evidence_id",
        "notes",
    },
    "torque_paths": {
        "path_id",
        "tool_item_id",
        "output_drive",
        "min_torque_nm",
        "max_torque_nm",
        "adapter_description",
        "adapter_max_torque_nm",
        "status",
        "evidence_id",
        "notes",
    },
    "spatial_profiles": {
        "profile_id",
        "location_id",
        "profile_json",
        "evidence_id",
        "sensitivity",
        "notes",
    },
    "kits": {"kit_id", "name", "serves_item_id", "evidence_id", "notes"},
    "kit_requirements": {
        "kit_id",
        "requirement_key",
        "item_id",
        "status",
        "evidence_id",
        "recorded_at",
        "verified_event_sequence",
        "notes",
    },
    "model_interfaces": {"model_id", "interface_id", "role", "evidence_id", "notes"},
    "valuations": {
        "valuation_id",
        "item_id",
        "amount",
        "currency",
        "valued_on",
        "basis",
        "evidence_id",
        "sensitivity",
        "notes",
    },
    "item_documents": {"document_id", "item_id", "document_type", "uri", "captured_on", "notes"},
    "item_dimensions": {
        "dimension_id",
        "item_id",
        "width",
        "height",
        "depth",
        "unit",
        "measured_on",
        "recorded_at",
        "evidence_id",
        "sensitivity",
        "notes",
    },
}


def location_assignment_error(
    locations: list[dict], location_id: object, container_id: object
) -> str | None:
    """Return why one stable location/container pair is physically incoherent."""
    if container_id is None:
        return None
    by_id = {row.get("location_id"): row for row in locations}
    container = by_id.get(container_id)
    location = by_id.get(location_id)
    if container is None or location is None:
        return "location or container does not exist"
    if container.get("kind") not in CONTAINER_LOCATION_KINDS:
        return "container_id must refer to a container, vehicle, or asset"
    if location.get("kind") == "unknown":
        return None
    current: dict | None = container
    visited: set[object] = set()
    while current is not None:
        current_id = current.get("location_id")
        if current_id == location_id:
            return None
        if current_id in visited:
            return "container ancestry contains a cycle"
        visited.add(current_id)
        current = by_id.get(current.get("parent_location_id"))
    return "container is not within the item's stable location"


def capture_provenance_failures(
    rows: dict[str, list[dict]], *, capture_session_id: str | None = None
) -> list[str]:
    """Validate durable capture provenance without consulting runtime staging."""
    failures: list[str] = []
    sessions: dict[object, list[dict]] = {}
    for row in rows["capture_sessions"]:
        sessions.setdefault(row.get("capture_session_id"), []).append(row)
    observations_by_session: dict[object, list[dict]] = {}
    for observation in rows["capture_observations"]:
        observations_by_session.setdefault(observation.get("capture_session_id"), []).append(
            observation
        )
    evidence_by_id: dict[object, list[dict]] = {}
    for row in rows["evidence"]:
        evidence_by_id.setdefault(row.get("evidence_id"), []).append(row)
    items_by_id: dict[object, list[dict]] = {}
    for row in rows["items"]:
        items_by_id.setdefault(row.get("item_id"), []).append(row)
    models_by_id: dict[object, list[dict]] = {}
    for row in rows["models"]:
        models_by_id.setdefault(row.get("model_id"), []).append(row)
    assets_by_digest: dict[object, list[dict]] = {}
    for asset in rows["media_assets"]:
        assets_by_digest.setdefault(asset.get("sha256"), []).append(asset)
    evidence_asset_links = rows["evidence_assets"]
    item_evidence_rows = rows["item_evidence"]
    events = rows["inventory_events"]
    locations_by_id = {row.get("location_id"): row for row in rows["locations"]}
    proposal_by_id: dict[object, list[dict]] = {}
    for row in rows["proposal_commits"]:
        proposal_by_id.setdefault(row.get("proposal_id"), []).append(row)

    def add(session_id: object, detail: str) -> None:
        failures.append(f"capture provenance {detail}: {session_id}")

    def strict_object(text: object, label: str) -> tuple[bytes, dict] | None:
        if not isinstance(text, str):
            return None
        try:
            payload = text.encode("utf-8")
            decoded = decode_json_bytes(payload, label)
        except (CaptureProvenanceError, UnicodeEncodeError):
            return None
        return (payload, decoded) if isinstance(decoded, dict) else None

    def region_link_matches(
        *, evidence_id: object, asset_id: object, role: str, region: object
    ) -> bool:
        matches = [
            link
            for link in evidence_asset_links
            if link.get("evidence_id") == evidence_id
            and link.get("asset_id") == asset_id
            and link.get("role") == role
        ]
        if len(matches) != 1:
            return False
        encoded = matches[0].get("region_json")
        if role == "source":
            return encoded is None
        try:
            decoded = (
                strict_json_loads(encoded, label="capture region")
                if isinstance(encoded, str)
                else None
            )
        except StrictJSONError:
            return False
        if decoded == region:
            return True
        return (
            isinstance(decoded, dict)
            and set(decoded) == {"regions"}
            and isinstance(decoded["regions"], list)
            and region in decoded["regions"]
        )

    def decision_location_sensitivity(physical: dict) -> str | None:
        sensitivities = ["low"]
        for location_id in (physical.get("location_id"), physical.get("container_id")):
            current = locations_by_id.get(location_id)
            visited: set[object] = set()
            while current is not None:
                current_id = current.get("location_id")
                if current_id in visited:
                    return None
                visited.add(current_id)
                sensitivity = current.get("sensitivity")
                if sensitivity not in SENSITIVITY_RANK:
                    return None
                sensitivities.append(sensitivity)
                current = locations_by_id.get(current.get("parent_location_id"))
        return max(sensitivities, key=SENSITIVITY_RANK.__getitem__)

    def decision_item_evidence(*, decision: dict, crop: dict) -> list[tuple[dict, dict]]:
        assets = assets_by_digest.get(crop.get("sha256"), [])
        if len(assets) != 1:
            return []
        asset_id = assets[0].get("asset_id")
        candidates: list[tuple[dict, dict]] = []
        for evidence in rows["evidence"]:
            if (
                evidence.get("evidence_type") != "physical_check"
                or evidence.get("claim_strength") != "explicit_current"
                or evidence.get("captured_on") != decision["physical"].get("checked_on")
                or not region_link_matches(
                    evidence_id=evidence.get("evidence_id"),
                    asset_id=asset_id,
                    role="crop",
                    region=crop.get("region"),
                )
            ):
                continue
            for link in item_evidence_rows:
                if link.get("evidence_id") == evidence.get("evidence_id"):
                    item_rows = items_by_id.get(link.get("item_id"), [])
                    if len(item_rows) == 1:
                        candidates.append((item_rows[0], evidence))
        return candidates

    def expected_decision_notes(decision: dict, crop: dict) -> str:
        notes = decision["physical"].get("notes")
        return (
            notes
            if isinstance(notes, str) and notes
            else (f"Physical verification from crop {crop['crop_id']}.")
        )

    def decision_model_matches(item: dict, discovery: dict) -> bool:
        model_rows = models_by_id.get(item.get("model_id"), [])
        if len(model_rows) != 1:
            return False
        model = model_rows[0]
        if (
            (
                discovery.get("existing_model_id") is not None
                and model.get("model_id") != discovery["existing_model_id"]
            )
            or (model.get("name") or "").casefold() != (discovery.get("name") or "").casefold()
            or (model.get("category") or "").casefold()
            != (discovery.get("category") or "").casefold()
            or (model.get("brand") or "").casefold() != (discovery.get("brand") or "").casefold()
            or (model.get("model") or "").casefold() != (discovery.get("model") or "").casefold()
            or (
                discovery.get("reference_url") is not None
                and model.get("reference_url") != discovery["reference_url"]
            )
        ):
            return False
        try:
            identifiers = strict_json_loads(
                model.get("identifiers_json"), label="capture decision identifiers"
            )
            specs = strict_json_loads(model.get("specs_json"), label="capture decision specs")
        except StrictJSONError:
            return False
        if discovery.get("new_model") is True:
            return identifiers == discovery.get("identifiers", {}) and specs == discovery.get(
                "specs", {}
            )
        return (not discovery.get("identifiers") or identifiers == discovery["identifiers"]) and (
            not discovery.get("specs") or specs == discovery["specs"]
        )

    for observation in rows["capture_observations"]:
        session_id = observation.get("capture_session_id")
        if capture_session_id is not None and session_id != capture_session_id:
            continue
        session_rows = sessions.get(session_id, [])
        session = session_rows[0] if len(session_rows) == 1 else None
        index = observation.get("observation_index")
        if type(index) is not int or index <= 0:
            add(session_id, "has invalid observation index")
        if session is not None and observation.get("evidence_id") != session.get("evidence_id"):
            add(session_id, "observation evidence disagrees with session")

    for session_id, session_rows in sessions.items():
        if capture_session_id is not None and session_id != capture_session_id:
            continue
        if len(session_rows) != 1:
            add(session_id, "does not resolve to exactly one session row")
            continue
        session = session_rows[0]
        state = session.get("provenance_state")
        bound_fields = (
            session.get("artifact_sha256"),
            session.get("artifact_json"),
            session.get("review_sha256"),
            session.get("review_json"),
        )
        session_observations = observations_by_session.get(session_id, [])
        indexes = [row.get("observation_index") for row in session_observations]
        if len(indexes) != len(set(indexes)):
            add(session_id, "has duplicate observation indexes")
        if state == "legacy_unbound":
            if any(value is not None for value in bound_fields):
                add(session_id, "legacy session contains invented bound fields")
            if any(row.get("validation_state") != "legacy_unknown" for row in session_observations):
                add(session_id, "legacy session contains a validated observation")
            continue
        if state != "bound" or any(not isinstance(value, str) for value in bound_fields):
            add(session_id, "has incomplete bound fields")
            continue
        artifact_pair = strict_object(session["artifact_json"], "capture artifact")
        review_pair = strict_object(session["review_json"], "capture review")
        if artifact_pair is None or review_pair is None:
            add(session_id, "contains malformed artifact or review JSON")
            continue
        artifact_wire, artifact = artifact_pair
        review_wire, review = review_pair
        try:
            validated_artifact = validate_capture_artifact(artifact, expected_session_id=session_id)
            validated_review = validate_capture_review(
                review,
                artifact=validated_artifact,
                expected_session_id=session_id,
            )
            artifact_canonical = canonical_artifact_bytes(validated_artifact)
            review_canonical = canonical_review_bytes(validated_review)
        except CaptureProvenanceError as error:
            add(session_id, f"fails artifact or review contract ({error})")
            continue
        if artifact_wire != artifact_canonical:
            add(session_id, "artifact wire bytes are not canonical")
        if review_wire != review_canonical:
            add(session_id, "review wire bytes are not canonical")
        artifact_digest = hashlib.sha256(artifact_wire).hexdigest()
        review_digest = hashlib.sha256(review_wire).hexdigest()
        if artifact_digest != session["artifact_sha256"]:
            add(session_id, "artifact digest disagrees with exact wire bytes")
        if review_digest != session["review_sha256"]:
            add(session_id, "review digest disagrees with exact wire bytes")
        if validated_review["artifact_sha256"] != session["artifact_sha256"]:
            add(session_id, "review artifact digest disagrees with session")
        if validated_artifact["captured_on"] != session.get("captured_on"):
            add(session_id, "captured_on disagrees with artifact")
        if SENSITIVITY_RANK.get(session.get("sensitivity"), -1) < SENSITIVITY_RANK.get(
            validated_artifact.get("sensitivity"), -1
        ):
            add(session_id, "sensitivity is lower than artifact")

        evidence_rows = evidence_by_id.get(session.get("evidence_id"), [])
        evidence = evidence_rows[0] if len(evidence_rows) == 1 else None
        if validated_artifact["evidence_id"] is not None:
            if (
                validated_artifact["evidence_id"] != session.get("evidence_id")
                or not isinstance(evidence, dict)
                or evidence.get("evidence_type") != validated_artifact["evidence_type"]
                or evidence.get("captured_on") != validated_artifact["captured_on"]
            ):
                add(session_id, "existing evidence binding disagrees with artifact")
        elif not (
            isinstance(evidence, dict)
            and evidence.get("source_ref") == validated_artifact["source_ref"]
            and evidence.get("evidence_type") == validated_artifact["evidence_type"]
            and evidence.get("captured_on") == validated_artifact["captured_on"]
            and evidence.get("claim_strength") == "research_only"
            and evidence.get("sensitivity") == session.get("sensitivity")
        ):
            add(session_id, "source-created evidence disagrees with artifact")

        referenced_item_ids = {
            candidate["item_id"]
            for ranking in validated_artifact["duplicate_candidates"].values()
            for candidate in ranking
        } | set(validated_review["links"].values())
        for item_id in referenced_item_ids:
            item_rows = items_by_id.get(item_id, [])
            if len(item_rows) != 1:
                add(session_id, f"item {item_id} does not resolve exactly once")
            elif SENSITIVITY_RANK.get(session.get("sensitivity"), -1) < SENSITIVITY_RANK.get(
                item_rows[0].get("sensitivity"), -1
            ):
                add(session_id, f"sensitivity is lower than embedded item {item_id}")

        crop_by_id = {crop["crop_id"]: crop for crop in validated_artifact["crops"]}
        decision_observation_items: dict[str, str] = {}
        for decision_index, decision in enumerate(validated_review["decisions"], start=1):
            crop = crop_by_id[decision["crop_id"]]
            physical = decision["physical"]
            candidates = decision_item_evidence(decision=decision, crop=crop)
            expected_item_id = decision.get("item_id")
            if expected_item_id is not None:
                candidates = [
                    pair for pair in candidates if pair[0].get("item_id") == expected_item_id
                ]
            else:
                discovery = decision["discovery"]
                candidates = [
                    pair for pair in candidates if decision_model_matches(pair[0], discovery)
                ]
            if len(candidates) != 1:
                add(
                    session_id,
                    f"decision {decision_index} does not bind exactly one physical item and crop",
                )
                continue
            item, physical_evidence = candidates[0]
            observation_id = decision.get("observation_id")
            if isinstance(observation_id, str) and observation_id.startswith("observation-"):
                linked_item_id = validated_review["links"].get(observation_id)
                if linked_item_id is not None and linked_item_id != item.get("item_id"):
                    add(
                        session_id,
                        f"decision {decision_index} conflicts with its observation link",
                    )
                decision_observation_items[observation_id] = item["item_id"]
            expected_notes = expected_decision_notes(decision, crop)
            expected_source_ref = (
                validated_artifact.get("source_ref")
                or f"capture:{validated_artifact['capture_session_id']}"
            )
            if (
                physical_evidence.get("source_ref") != expected_source_ref
                or physical_evidence.get("notes") != expected_notes
            ):
                add(session_id, f"decision {decision_index} physical evidence details disagree")
            required_sensitivity = decision_location_sensitivity(physical)
            if required_sensitivity is None:
                add(session_id, f"decision {decision_index} has invalid location ancestry")
                continue
            required_sensitivity = max(
                (required_sensitivity, item.get("sensitivity", "low")),
                key=SENSITIVITY_RANK.__getitem__,
            )
            if (
                SENSITIVITY_RANK.get(session.get("sensitivity"), -1)
                < SENSITIVITY_RANK[required_sensitivity]
                or SENSITIVITY_RANK.get(physical_evidence.get("sensitivity"), -1)
                < SENSITIVITY_RANK[required_sensitivity]
            ):
                add(session_id, f"decision {decision_index} sensitivity is too low")
            physically_verified = [
                event
                for event in events
                if event.get("item_id") == item.get("item_id")
                and event.get("evidence_id") == physical_evidence.get("evidence_id")
                and event.get("event_type") == "physically_verified"
                and event.get("occurred_on") == physical.get("checked_on")
                and event.get("location_id") == physical.get("location_id")
                and event.get("container_id") == physical.get("container_id")
                and event.get("actor") == physical.get("actor")
                and event.get("notes") == expected_notes
            ]
            if len(physically_verified) != 1:
                add(session_id, f"decision {decision_index} lacks its physical verification event")
            if (
                physical.get("condition") is not None
                and item.get("condition") != physical["condition"]
            ):
                add(session_id, f"decision {decision_index} condition disagrees with item")
            if (
                physical.get("serial_or_lot") is not None
                and item.get("serial_or_lot") != physical["serial_or_lot"]
            ):
                add(session_id, f"decision {decision_index} serial disagrees with item")
            if physical.get("quantity") is not None:
                if item.get("quantity") != physical["quantity"]:
                    add(session_id, f"decision {decision_index} quantity disagrees with item")
                if physical.get("unit") is not None and item.get("unit") != physical["unit"]:
                    add(session_id, f"decision {decision_index} unit disagrees with item")
            if decision.get("discovery") is not None:
                discovery_sensitivity = max(
                    (discovery.get("sensitivity", "personal"), required_sensitivity),
                    key=SENSITIVITY_RANK.__getitem__,
                )
                if item.get("sensitivity") != discovery_sensitivity:
                    add(
                        session_id,
                        f"decision {decision_index} discovery sensitivity disagrees with item",
                    )
                received = [
                    event
                    for event in events
                    if event.get("item_id") == item.get("item_id")
                    and event.get("evidence_id") == physical_evidence.get("evidence_id")
                    and event.get("event_type") == "received"
                    and event.get("occurred_on") == physical.get("checked_on")
                    and event.get("actor") == physical.get("actor")
                    and event.get("notes") == expected_notes
                    and event.get("location_id") == physical.get("location_id")
                    and event.get("container_id") == physical.get("container_id")
                ]
                if len(received) != 1:
                    add(session_id, f"decision {decision_index} discovery lacks its received event")

        media_entries = [
            ("source", validated_artifact["source"]),
            *(("crop", crop) for crop in validated_artifact["crops"]),
        ]
        for role, entry in media_entries:
            assets = assets_by_digest.get(entry.get("sha256"), [])
            if len(assets) != 1:
                add(session_id, f"{role} digest does not resolve to one media asset")
                continue
            asset = assets[0]
            if asset.get("byte_size") != entry.get("byte_length") or asset.get(
                "media_type"
            ) != entry.get("content_type"):
                add(session_id, f"{role} media metadata disagrees with artifact")
            if asset.get("sensitivity") != session.get("sensitivity"):
                add(session_id, f"{role} media sensitivity disagrees with session")
            if not region_link_matches(
                evidence_id=session.get("evidence_id"),
                asset_id=asset.get("asset_id"),
                role=role,
                region=entry.get("region"),
            ):
                add(session_id, f"{role} media lacks exact evidence binding")

        expected_observations = validated_artifact["observations"]
        if sorted(indexes) != list(range(1, len(expected_observations) + 1)):
            add(session_id, "observation indexes do not cover the artifact")
            continue
        by_index = {row["observation_index"]: row for row in session_observations}
        for index, expected in enumerate(expected_observations, start=1):
            observation = by_index[index]
            if observation.get("validation_state") != "validated":
                add(session_id, f"observation {index} is not validated")
                continue
            parsed = strict_object(
                observation.get("observation_json"),
                f"capture observation {index}",
            )
            if parsed is None:
                add(session_id, f"observation {index} is malformed")
                continue
            observation_wire, observation_value = parsed
            try:
                normalized = normalize_observation(observation_value)
                expected_wire = json.dumps(
                    normalized.to_dict(),
                    ensure_ascii=False,
                    sort_keys=True,
                    separators=(",", ":"),
                    allow_nan=False,
                ).encode("utf-8")
            except (CaptureError, TypeError, ValueError):
                add(session_id, f"observation {index} fails adapter contract")
                continue
            if observation_wire != expected_wire or observation_value != expected:
                add(session_id, f"observation {index} disagrees with artifact")
            observation_key = f"observation-{index}"
            expected_item_id = decision_observation_items.get(
                observation_key,
                validated_review["links"].get(observation_key),
            )
            if observation.get("item_id") != expected_item_id:
                add(session_id, f"observation {index} link disagrees with review")

        receipt_rows = proposal_by_id.get(validated_review["proposal_id"], [])
        receipt = receipt_rows[0] if len(receipt_rows) == 1 else None
        expected_operations = [["capture-commit", "--capture-session-id", str(session_id)]]
        expected_operations_digest = hashlib.sha256(
            json.dumps(
                expected_operations,
                ensure_ascii=False,
                separators=(",", ":"),
            ).encode("utf-8")
        ).hexdigest()
        if not (
            isinstance(receipt, dict)
            and receipt.get("base_digest") == validated_review["base_digest"]
            and receipt.get("operations_digest") == expected_operations_digest
        ):
            add(session_id, "has no matching canonical proposal receipt")

    return failures


def semantic_failures(rows: dict[str, list[dict]]) -> list[str]:
    """Reject current rows that SQLite's types and foreign keys cannot express."""
    failures: list[str] = []
    evidence_sensitivity = {
        row.get("evidence_id"): row.get("sensitivity") for row in rows["evidence"]
    }
    evidence_by_id = {row.get("evidence_id"): row for row in rows["evidence"]}
    media_assets_by_id = {row.get("asset_id"): row for row in rows["media_assets"]}
    item_sensitivity = {row.get("item_id"): row.get("sensitivity") for row in rows["items"]}
    item_evidence = {(row.get("item_id"), row.get("evidence_id")) for row in rows["item_evidence"]}
    evidence_items: dict[object, set[object]] = {}

    locations_by_id = {row.get("location_id"): row for row in rows["locations"]}
    reported_location_cycles: set[object] = set()
    for origin in rows["locations"]:
        current: dict | None = origin
        visited: set[object] = set()
        while current is not None:
            current_id = current.get("location_id")
            if current_id in visited:
                if current_id not in reported_location_cycles:
                    failures.append(f"location hierarchy contains a cycle: {current_id}")
                    reported_location_cycles.add(current_id)
                break
            visited.add(current_id)
            parent_id = current.get("parent_location_id")
            current = locations_by_id.get(parent_id)
            if parent_id is not None and current is None:
                failures.append(f"location has a missing parent: {origin.get('location_id')}")
                break

    for item in rows["items"]:
        identity_sensitivity = item.get("identity_sensitivity") or item.get("sensitivity")
        if (
            identity_sensitivity not in SENSITIVITY_RANK
            or item.get("sensitivity") not in SENSITIVITY_RANK
            or SENSITIVITY_RANK[identity_sensitivity] < SENSITIVITY_RANK[item["sensitivity"]]
        ):
            failures.append(
                f"item identity sensitivity is lower than the item: {item.get('item_id')}"
            )
        if error := location_assignment_error(
            rows["locations"], item.get("location_id"), item.get("container_id")
        ):
            failures.append(
                f"item has an invalid location/container assignment: "
                f"{item.get('item_id')} ({error})"
            )
        quantity = item.get("quantity")
        active_state = item.get("ownership_state") not in {
            "not_owned",
            "disposed",
            "refunded",
        }
        if quantity is not None and (
            isinstance(quantity, bool)
            or not isinstance(quantity, (int, float))
            or not math.isfinite(float(quantity))
            or float(quantity) < 0
            or (active_state and float(quantity) <= 0)
        ):
            failures.append(f"item has invalid quantity: {item.get('item_id')}")
        for amount_field, currency_field in (
            ("purchase_price", "purchase_currency"),
            ("replacement_value", "value_currency"),
        ):
            amount = item.get(amount_field)
            currency = item.get(currency_field)
            if (amount is None) != (currency is None):
                failures.append(
                    f"item has unpaired {amount_field}/{currency_field}: {item.get('item_id')}"
                )
                continue
            if amount is not None and (
                isinstance(amount, bool)
                or not isinstance(amount, (int, float))
                or not math.isfinite(float(amount))
                or float(amount) < 0
                or not isinstance(currency, str)
                or re.fullmatch(r"[A-Z]{3}", currency) is None
            ):
                failures.append(
                    f"item has invalid {amount_field}/{currency_field}: {item.get('item_id')}"
                )

    chronological_by_item: dict[object, list[dict]] = {}
    quantity_events_by_item: dict[object, list[dict]] = {}
    for event in rows["inventory_events"]:
        event_id = event.get("event_id")
        event_type = event.get("event_type")
        occurred_on = event.get("occurred_on")
        observed_on = event.get("observed_on")
        occurred_on_precision = event.get("occurred_on_precision")
        try:
            canonical_observation_date = (
                isinstance(observed_on, str)
                and date.fromisoformat(observed_on).isoformat() == observed_on
            )
            canonical_occurrence_date = (
                isinstance(occurred_on, str)
                and date.fromisoformat(occurred_on).isoformat() == occurred_on
            )
        except ValueError:
            canonical_observation_date = False
            canonical_occurrence_date = False
        if not canonical_observation_date:
            failures.append(f"inventory event has non-canonical observation date: {event_id}")
        if occurred_on_precision not in {"exact", "unknown"}:
            failures.append(f"inventory event has invalid date precision: {event_id}")
        elif occurred_on_precision == "exact" and not canonical_occurrence_date:
            failures.append(f"inventory event has non-canonical occurrence date: {event_id}")
        elif (
            occurred_on_precision == "exact"
            and canonical_observation_date
            and observed_on < occurred_on
        ):
            failures.append(f"inventory event was observed before it occurred: {event_id}")
        elif occurred_on_precision == "unknown" and occurred_on is not None:
            failures.append(f"unknown-date inventory event asserts a date: {event_id}")
        evidence = evidence_by_id.get(event.get("evidence_id"))
        expected_claim = EVENT_CLAIM_REQUIREMENTS.get(event_type)
        if expected_claim is not None and (
            evidence is None or evidence.get("claim_strength") != expected_claim
        ):
            failures.append(f"inventory event has invalid ownership evidence: {event_id}")
        expected_evidence_type = EVENT_EVIDENCE_TYPE_REQUIREMENTS.get(event_type)
        if expected_evidence_type is not None and (
            evidence is None or evidence.get("evidence_type") != expected_evidence_type
        ):
            failures.append(f"inventory event has invalid evidence type: {event_id}")
        details_raw = event.get("details_json")
        details: object = None
        if details_raw is not None:
            try:
                details = strict_json_loads(
                    details_raw, label=f"inventory event {event_id} details_json"
                )
            except StrictJSONError:
                failures.append(f"inventory event has invalid details: {event_id}")
            else:
                if (
                    not isinstance(details, dict)
                    or strict_json_dumps(details, sort_keys=True) != details_raw
                ):
                    failures.append(f"inventory event has non-canonical details: {event_id}")
        quantity_payload_event = event_type == "quantity_changed" or (
            event_type in {"planned", "ordered"} and details_raw is not None
        )
        if quantity_payload_event:
            if event.get("context_quality") == "bound":
                if not isinstance(details, dict) or set(details) != {
                    "previous_quantity",
                    "previous_unit",
                    "quantity",
                    "unit",
                }:
                    failures.append(f"bound quantity event lacks a replayable payload: {event_id}")
                else:
                    previous_quantity = details["previous_quantity"]
                    quantity = details["quantity"]
                    if (
                        event_type in {"planned", "ordered"}
                        and details["previous_unit"] != details["unit"]
                    ):
                        failures.append(
                            f"purchase-only quantity event changes unit: {event_id}"
                        )
                    if (
                        any(
                            value is not None
                            and (
                                type(value) not in {int, float}
                                or not math.isfinite(value)
                                or value <= 0
                            )
                            for value in (previous_quantity, quantity)
                        )
                        or quantity is None
                    ):
                        failures.append(f"quantity event has invalid numeric payload: {event_id}")
                    if (
                        not all(
                            value is None or (isinstance(value, str) and value.strip())
                            for value in (details["previous_unit"], details["unit"])
                        )
                        or details["unit"] is None
                    ):
                        failures.append(f"quantity event has invalid unit payload: {event_id}")
                    quantity_events_by_item.setdefault(event.get("item_id"), []).append(
                        {**event, "_details": details}
                    )
        elif event_type == "reacquired":
            if (
                not isinstance(details, dict)
                or set(details)
                != {"condition_checked", "quantity_checked", "reset_fields", "unit"}
                or details.get("reset_fields") != list(REACQUISITION_RESET_FIELDS)
                or not (
                    details.get("condition_checked") is None
                    or (
                        isinstance(details.get("condition_checked"), str)
                        and details["condition_checked"].strip()
                    )
                )
                or (
                    details.get("quantity_checked") is None
                    and details.get("unit") is not None
                )
                or (
                    details.get("quantity_checked") is not None
                    and (
                        type(details["quantity_checked"]) not in {int, float}
                        or not math.isfinite(details["quantity_checked"])
                        or details["quantity_checked"] <= 0
                        or not isinstance(details.get("unit"), str)
                        or not details["unit"].strip()
                    )
                )
            ):
                failures.append(
                    f"reacquired event lacks an exact episode-reset declaration: {event_id}"
                )
        elif details_raw is not None:
            failures.append(f"non-quantity event has unexpected details: {event_id}")
        if event_type in CHRONOLOGICAL_EVENT_TYPES:
            chronological_by_item.setdefault(event.get("item_id"), []).append(event)
    for item_id, events in chronological_by_item.items():
        latest_exact: dict | None = None
        for current in sorted(events, key=lambda row: row.get("sequence", -1)):
            if current.get("occurred_on_precision") == "exact":
                if latest_exact is not None and current.get(
                    "occurred_on", ""
                ) < latest_exact.get("occurred_on", ""):
                    failures.append(
                        "item lifecycle dates move backwards: "
                        f"{item_id} ({latest_exact.get('event_id')} -> "
                        f"{current.get('event_id')})"
                    )
                latest_exact = current
            elif latest_exact is not None and current.get(
                "observed_on", ""
            ) < latest_exact.get("occurred_on", ""):
                failures.append(
                    "unknown-date lifecycle observation predates later exact reality: "
                    f"{item_id} ({latest_exact.get('event_id')} -> "
                    f"{current.get('event_id')})"
                )
    items_for_quantities = {row.get("item_id"): row for row in rows["items"]}
    for item_id, events in quantity_events_by_item.items():
        ordered = sorted(events, key=lambda row: row.get("sequence", -1))
        for previous, current in zip(ordered, ordered[1:]):
            if previous["_details"].get("quantity") != current["_details"].get(
                "previous_quantity"
            ) or previous["_details"].get("unit") != current["_details"].get("previous_unit"):
                failures.append(f"quantity-event chain is broken: {item_id}")
        latest = ordered[-1]["_details"]
        current_item = items_for_quantities.get(item_id, {})
        if latest.get("quantity") != current_item.get("quantity") or latest.get(
            "unit"
        ) != current_item.get("unit"):
            failures.append(f"item quantity disagrees with latest quantity event: {item_id}")

    def supports(evidence_id: object, *item_ids: object) -> None:
        if evidence_id is not None:
            evidence_items.setdefault(evidence_id, set()).update(
                item_id for item_id in item_ids if item_id is not None
            )

    for item_id, evidence_id in item_evidence:
        supports(evidence_id, item_id)
    for event in rows["inventory_events"]:
        supports(event.get("evidence_id"), event.get("item_id"))
    for relationship in rows["relationships"]:
        supports(
            relationship.get("evidence_id"),
            relationship.get("subject_item_id"),
            relationship.get("object_item_id"),
        )
    for kit in rows["kits"]:
        supports(kit.get("evidence_id"), kit.get("serves_item_id"))
    for path in rows["torque_paths"]:
        supports(path.get("evidence_id"), path.get("tool_item_id"))
    for requirement in rows["kit_requirements"]:
        supports(requirement.get("evidence_id"), requirement.get("item_id"))
    kits_for_reviews = {row.get("kit_id"): row for row in rows["kits"]}
    for review in rows["kit_reviews"]:
        kit = kits_for_reviews.get(review.get("kit_id"), {})
        supports(review.get("evidence_id"), kit.get("serves_item_id"))
    items_by_model: dict[object, set[object]] = {}
    for item in rows["items"]:
        items_by_model.setdefault(item.get("model_id"), set()).add(item.get("item_id"))
    for link in rows["model_interfaces"]:
        supports(link.get("evidence_id"), *items_by_model.get(link.get("model_id"), set()))
    session_sensitivity = {
        row.get("capture_session_id"): row.get("sensitivity") for row in rows["capture_sessions"]
    }
    maintenance_items: dict[object, set[object]] = {}
    for link in rows["maintenance_session_items"]:
        maintenance_items.setdefault(link.get("maintenance_session_id"), set()).add(
            link.get("item_id")
        )

    def rank(value: object) -> int | None:
        return SENSITIVITY_RANK.get(value) if isinstance(value, str) else None

    def location_ancestry_sensitivity(location_id: object) -> str | None:
        current = locations_by_id.get(location_id)
        seen: set[object] = set()
        selected: str | None = None
        while current is not None:
            current_id = current.get("location_id")
            if current_id in seen:
                return None
            seen.add(current_id)
            sensitivity = current.get("sensitivity")
            if rank(sensitivity) is None:
                return None
            if selected is None or SENSITIVITY_RANK[sensitivity] > SENSITIVITY_RANK[selected]:
                selected = sensitivity
            current = locations_by_id.get(current.get("parent_location_id"))
        return selected

    def require_not_lower(
        table: str, row: dict, row_id: object, *parents: tuple[str, object]
    ) -> None:
        row_rank = rank(row.get("sensitivity"))
        if row_rank is None:
            failures.append(f"{table} has invalid sensitivity: {row_id}")
            return
        for label, parent_sensitivity in parents:
            parent_rank = rank(parent_sensitivity)
            if parent_rank is not None and row_rank < parent_rank:
                failures.append(f"{table} sensitivity is lower than {label}: {row_id}")

    location_events_by_item: dict[object, list[dict]] = {}
    for event in rows["inventory_events"]:
        event_type = event.get("event_type")
        event_id = event.get("event_id")
        location_id = event.get("location_id")
        container_id = event.get("container_id")
        area_location_id = event.get("area_location_id")
        context_quality = event.get("context_quality")
        if context_quality == "legacy_unknown" and any(
            value is not None for value in (location_id, container_id, area_location_id)
        ):
            failures.append(f"legacy event contains invented location context: {event_id}")
        if context_quality == "bound" and event_type in LOCATION_CONTEXT_EVENT_TYPES:
            if location_id is None:
                failures.append(f"location-bearing event has no location: {event_id}")
            else:
                location_events_by_item.setdefault(event.get("item_id"), []).append(event)
                if error := location_assignment_error(rows["locations"], location_id, container_id):
                    failures.append(
                        f"inventory event has invalid location context: {event_id} ({error})"
                    )
                location = locations_by_id.get(location_id)
                if (
                    event_type in STABLE_PHYSICAL_LOCATION_EVENT_TYPES
                    and location is not None
                    and location.get("kind") == "unknown"
                ):
                    failures.append(f"physical ownership event has no stable location: {event_id}")
        if (
            context_quality == "bound"
            and event_type == "not_found_in_area"
            and area_location_id is None
        ):
            failures.append(f"not-found event has no checked area: {event_id}")
        evidence = evidence_by_id.get(event.get("evidence_id"), {})
        evidence_rank = rank(evidence.get("sensitivity"))
        for reference in (location_id, container_id, area_location_id):
            context_sensitivity = location_ancestry_sensitivity(reference)
            context_rank = rank(context_sensitivity)
            if (
                context_rank is not None
                and evidence_rank is not None
                and evidence_rank < context_rank
            ):
                failures.append(
                    f"inventory event evidence is lower than location context: {event_id}"
                )

    current_locations = {
        row.get("item_id"): (row.get("location_id"), row.get("container_id"))
        for row in rows["items"]
        if row.get("ownership_state") not in {"disposed", "refunded", "not_owned"}
    }
    for item_id, events in location_events_by_item.items():
        latest = max(events, key=lambda row: row.get("sequence", -1))
        if item_id in current_locations and current_locations[item_id] != (
            latest.get("location_id"),
            latest.get("container_id"),
        ):
            failures.append(f"item location disagrees with its latest location event: {item_id}")

    def require_date(table: str, row: dict, row_id: object, field: str) -> None:
        value = row.get(field)
        try:
            valid = isinstance(value, str) and date.fromisoformat(value).isoformat() == value
        except ValueError:
            valid = False
        if not valid:
            failures.append(f"{table} has non-canonical {field}: {row_id}")

    def require_timestamp(table: str, row: dict, row_id: object, field: str) -> None:
        value = row.get(field)
        try:
            parsed = datetime.fromisoformat(value) if isinstance(value, str) else None
            valid = parsed is not None and parsed.tzinfo is not None and parsed.isoformat() == value
        except ValueError:
            valid = False
        if not valid:
            failures.append(f"{table} has non-canonical {field}: {row_id}")

    def require_json_type(
        table: str,
        row: dict,
        row_id: object,
        field: str,
        expected_type: type,
    ) -> None:
        try:
            value = row.get(field)
            valid = isinstance(value, str) and isinstance(
                strict_json_loads(value, label=f"{table} {field}"), expected_type
            )
        except StrictJSONError:
            valid = False
        if not valid:
            failures.append(
                f"{table} {field} must be strict JSON {expected_type.__name__}: {row_id}"
            )

    for row in rows["models"]:
        row_id = row.get("model_id")
        require_json_type("model", row, row_id, "specs_json", dict)
        require_json_type("model", row, row_id, "interfaces_json", list)
        require_json_type("model", row, row_id, "identifiers_json", dict)
        try:
            legacy_interfaces = strict_json_loads(
                row.get("interfaces_json"), label="model interfaces_json"
            )
        except StrictJSONError:
            legacy_interfaces = None
        if isinstance(legacy_interfaces, list) and any(
            not isinstance(value, str) or not value.strip() for value in legacy_interfaces
        ):
            failures.append(f"model interfaces_json entries must be non-empty strings: {row_id}")
    for row in rows["interfaces"]:
        require_json_type("interface", row, row.get("interface_id"), "properties_json", dict)
    for row in rows["evidence_assets"]:
        row_id = f"{row.get('evidence_id')}/{row.get('asset_id')}/{row.get('role')}"
        if row.get("region_json") is not None:
            require_json_type(
                "evidence asset",
                row,
                row_id,
                "region_json",
                dict,
            )
        evidence = evidence_by_id.get(row.get("evidence_id"), {})
        asset = media_assets_by_id.get(row.get("asset_id"), {})
        media_type = asset.get("media_type")
        insurance_document = isinstance(media_type, str) and (
            media_type.split(";", 1)[0].strip().casefold() == "application/pdf"
            or media_type.split(";", 1)[0].strip().casefold().startswith("image/")
        )
        if row.get("role") == "receipt" and (
            evidence.get("claim_strength") != "purchase_only"
            or evidence.get("evidence_type") not in {"merchant_account", "user_source"}
            or not insurance_document
        ):
            failures.append(f"receipt media lacks purchase-document evidence semantics: {row_id}")
        if row.get("role") == "appraisal" and (
            evidence.get("claim_strength") != "research_only"
            or evidence.get("evidence_type") not in {"user_source", "vault_note"}
            or not insurance_document
        ):
            failures.append(f"appraisal media lacks reviewed-document evidence semantics: {row_id}")

    kits_by_id = {row.get("kit_id"): row for row in rows["kits"]}
    items_by_id = {row.get("item_id"): row for row in rows["items"]}
    for row in rows["relationships"]:
        row_id = row.get("relationship_id")
        evidence_id = row.get("evidence_id")
        for endpoint in (row.get("subject_item_id"), row.get("object_item_id")):
            if (endpoint, evidence_id) not in item_evidence:
                failures.append(f"relationship evidence does not support endpoint item: {row_id}")
    for row in rows["model_interfaces"]:
        row_id = f"{row.get('model_id')}/{row.get('interface_id')}/{row.get('role')}"
        model_item_ids = items_by_model.get(row.get("model_id"), set())
        if not model_item_ids or not any(
            (item_id, row.get("evidence_id")) in item_evidence for item_id in model_item_ids
        ):
            failures.append(
                f"model-interface evidence does not support an item of the model: {row_id}"
            )
    for row in rows["kits"]:
        if (row.get("serves_item_id"), row.get("evidence_id")) not in item_evidence:
            failures.append(f"kit evidence does not support served item: {row.get('kit_id')}")
    for row in rows["kit_requirements"]:
        row_id = f"{row.get('kit_id')}/{row.get('requirement_key')}"
        require_timestamp("kit requirement", row, row_id, "recorded_at")
        evidence_id = row.get("evidence_id")
        assigned_item_id = row.get("item_id")
        if (
            assigned_item_id is not None
            and (
                assigned_item_id,
                evidence_id,
            )
            not in item_evidence
        ):
            failures.append(f"kit requirement evidence does not support assigned item: {row_id}")
        status = row.get("status")
        verified_sequence = row.get("verified_event_sequence")
        assigned_item = items_by_id.get(assigned_item_id, {})
        supporting_evidence = evidence_by_id.get(evidence_id, {})
        if status in {"source_present", "exists_unassigned"} and (
            assigned_item_id is None
            or assigned_item.get("ownership_state") != "confirmed"
            or supporting_evidence.get("claim_strength") != "explicit_current"
        ):
            failures.append(f"current kit requirement lacks current possession proof: {row_id}")
        if (
            status == "source_present"
            and supporting_evidence.get("evidence_type") != "physical_check"
        ):
            failures.append(f"source-present kit requirement lacks physical evidence: {row_id}")
        if status == "not_recorded" and assigned_item_id is not None:
            failures.append(f"not-recorded kit requirement unexpectedly assigns an item: {row_id}")
        if status in {"source_present", "exists_unassigned"}:
            matching_events = [
                event
                for event in rows["inventory_events"]
                if event.get("sequence") == verified_sequence
                and event.get("item_id") == assigned_item_id
                and event.get("evidence_id") == evidence_id
                and (status != "source_present" or event.get("event_type") == "physically_verified")
            ]
            if len(matching_events) != 1:
                failures.append(
                    f"current kit requirement has no matching ownership event: {row_id}"
                )
        elif verified_sequence is not None:
            failures.append(f"non-current kit requirement has an ownership-event marker: {row_id}")
    for row in rows["kit_reviews"]:
        row_id = row.get("review_id")
        kit = kits_by_id.get(row.get("kit_id"), {})
        served_item_id = kit.get("serves_item_id")
        evidence_id = row.get("evidence_id")
        evidence = evidence_by_id.get(evidence_id, {})
        require_date("kit review", row, row_id, "reviewed_on")
        require_timestamp("kit review", row, row_id, "recorded_at")
        if not isinstance(row.get("actor"), str) or not row["actor"].strip():
            failures.append(f"kit review has blank actor: {row_id}")
        if (served_item_id, evidence_id) not in item_evidence:
            failures.append(f"kit-review evidence does not support served item: {row_id}")
        try:
            keys = strict_json_loads(
                row.get("requirement_keys_json"),
                label="kit review requirement_keys_json",
            )
        except StrictJSONError:
            keys = None
        if (
            not isinstance(keys, list)
            or any(not isinstance(key, str) or not key.strip() for key in keys)
            or keys != sorted(set(keys))
            or (
                isinstance(keys, list)
                and strict_json_dumps(keys, sort_keys=True) != row.get("requirement_keys_json")
            )
            or (row.get("completeness") == "complete" and not keys)
        ):
            failures.append(f"kit review has invalid requirement keys: {row_id}")
        if (
            evidence.get("evidence_type") != "user_source"
            or evidence.get("claim_strength") != "research_only"
            or evidence.get("captured_on") != row.get("reviewed_on")
            or not isinstance(evidence.get("source_ref"), str)
            or not evidence["source_ref"].strip()
        ):
            failures.append(f"kit review lacks dedicated review evidence: {row_id}")
        if isinstance(keys, list):
            evidence_identity = strict_json_dumps(
                {
                    "completeness": row.get("completeness"),
                    "kit_id": row.get("kit_id"),
                    "requirement_keys": keys,
                    "reviewed_on": row.get("reviewed_on"),
                    "source_ref": evidence.get("source_ref"),
                },
                sort_keys=True,
            )
            expected_evidence_id = (
                "ev-kit-review-" + hashlib.sha256(evidence_identity.encode()).hexdigest()[:24]
            )
            if evidence_id != expected_evidence_id:
                failures.append(f"kit review evidence is not review-bound: {row_id}")
        require_not_lower(
            "kit review",
            row,
            row_id,
            ("served item", item_sensitivity.get(served_item_id)),
            ("evidence", evidence_sensitivity.get(evidence_id)),
        )
    for row in rows["torque_paths"]:
        row_id = row.get("path_id")
        evidence_id = row.get("evidence_id")
        if (row.get("tool_item_id"), evidence_id) not in item_evidence:
            failures.append(f"torque-path evidence does not support tool item: {row_id}")
        minimum = row.get("min_torque_nm")
        maximum = row.get("max_torque_nm")
        adapter_maximum = row.get("adapter_max_torque_nm")
        paired_range = (minimum is None) == (maximum is None)
        valid_range = paired_range and (
            minimum is None
            or (
                type(minimum) in {int, float}
                and type(maximum) in {int, float}
                and math.isfinite(minimum)
                and math.isfinite(maximum)
                and 0 <= minimum <= maximum
            )
        )
        valid_adapter_maximum = adapter_maximum is None or (
            type(adapter_maximum) in {int, float}
            and math.isfinite(adapter_maximum)
            and adapter_maximum >= 0
        )
        adapter_description = row.get("adapter_description")
        has_adapter_description = isinstance(adapter_description, str) and bool(
            adapter_description.strip()
        )
        status = row.get("status")
        status_shape_valid = (
            status == "direct"
            and minimum is not None
            and adapter_description is None
            and adapter_maximum is None
            or status == "adapter_rating_unknown"
            and minimum is not None
            and has_adapter_description
            and adapter_maximum is None
            or status == "attachment_only"
            and minimum is None
            and has_adapter_description
            or status == "needs_verification"
            and (adapter_maximum is None or has_adapter_description)
        )
        if not valid_range or not valid_adapter_maximum or not status_shape_valid:
            failures.append(f"torque path has unsafe or incomplete limits: {row_id}")

    normalized_tags: set[tuple[object, str]] = set()
    for row in rows["item_tags"]:
        tag = row.get("tag")
        key = (row.get("item_id"), tag.casefold() if isinstance(tag, str) else "")
        if (
            not isinstance(tag, str)
            or not re.fullmatch(r"[a-z0-9]+(?:-[a-z0-9]+)*", tag)
            or len(tag) > 64
        ):
            failures.append(f"private classification tag is not canonical: {row.get('item_id')}")
        if key in normalized_tags:
            failures.append(
                f"duplicate case-insensitive private classification tag: {row.get('item_id')}"
            )
        normalized_tags.add(key)
        item_id = row.get("item_id")
        evidence_id = row.get("evidence_id")
        if (item_id, evidence_id) not in item_evidence:
            failures.append(f"classification evidence does not support item: {item_id}/{tag}")
        require_not_lower(
            "classification",
            row,
            f"{item_id}/{tag}",
            ("item", item_sensitivity.get(item_id)),
            ("evidence", evidence_sensitivity.get(evidence_id)),
        )

    for evidence_id, sensitivity in evidence_sensitivity.items():
        if rank(sensitivity) is None:
            failures.append(f"evidence has invalid sensitivity: {evidence_id}")
        for item_id in evidence_items.get(evidence_id, set()):
            if (
                rank(sensitivity) is not None
                and rank(item_sensitivity.get(item_id)) is not None
                and rank(sensitivity) < rank(item_sensitivity.get(item_id))
            ):
                failures.append(f"evidence sensitivity is lower than supported item: {evidence_id}")
    for row in rows["aliases"]:
        row_id, item_id, evidence_id = (
            row.get("alias_id"),
            row.get("item_id"),
            row.get("evidence_id"),
        )
        if (item_id, evidence_id) not in item_evidence:
            failures.append(f"alias evidence does not support item: {row_id}")
        require_not_lower(
            "alias",
            row,
            row_id,
            ("item", item_sensitivity.get(item_id)),
            ("evidence", evidence_sensitivity.get(evidence_id)),
        )
    for row in rows["spatial_profiles"]:
        row_id = row.get("profile_id")
        supporting = evidence_by_id.get(row.get("evidence_id"), {})
        expected_claim = {
            "physical_check": "explicit_current",
            "research": "research_only",
            "vault_note": "research_only",
        }.get(supporting.get("evidence_type"))
        if expected_claim is None or supporting.get("claim_strength") != expected_claim:
            failures.append(f"spatial profile has invalid geometry evidence: {row_id}")
        try:
            profile = strict_json_loads(
                row.get("profile_json"), label="spatial profile profile_json"
            )
            normalize_spatial_profile(profile)
        except (TypeError, StrictJSONError, SpatialValidationError):
            failures.append(
                f"spatial profile profile_json has an invalid canonical shape: {row_id}"
            )
        require_not_lower(
            "spatial profile",
            row,
            row_id,
            (
                "location ancestry",
                location_ancestry_sensitivity(row.get("location_id")),
            ),
            ("evidence", evidence_sensitivity.get(row.get("evidence_id"))),
        )
    for row in rows["valuations"]:
        row_id, item_id, evidence_id = (
            row.get("valuation_id"),
            row.get("item_id"),
            row.get("evidence_id"),
        )
        amount = row.get("amount")
        if type(amount) not in {int, float} or not math.isfinite(amount) or amount < 0:
            failures.append(f"valuation has invalid amount: {row_id}")
        if (
            not isinstance(row.get("currency"), str)
            or re.fullmatch(r"[A-Z]{3}", row["currency"]) is None
        ):
            failures.append(f"valuation has invalid currency: {row_id}")
        if row.get("basis") not in {
            "replacement",
            "appraisal",
            "purchase",
            "receipt",
            "market",
            "other",
        }:
            failures.append(f"valuation has invalid basis: {row_id}")
        elif not valuation_evidence_supports_basis(
            evidence_by_id.get(evidence_id, {}), row.get("basis")
        ):
            failures.append(f"valuation evidence has incompatible semantics: {row_id}")
        require_date("valuation", row, row_id, "valued_on")
        if (item_id, evidence_id) not in item_evidence:
            failures.append(f"valuation evidence does not support item: {row_id}")
        require_not_lower(
            "valuation",
            row,
            row_id,
            ("item", item_sensitivity.get(item_id)),
            ("evidence", evidence_sensitivity.get(evidence_id)),
        )
    for row in rows["item_dimensions"]:
        row_id = row.get("dimension_id")
        item_id = row.get("item_id")
        evidence_id = row.get("evidence_id")
        values = [row.get(field) for field in ("width", "height", "depth")]
        if not any(value is not None for value in values) or any(
            value is not None
            and (type(value) not in {int, float} or not math.isfinite(value) or value <= 0)
            for value in values
        ):
            failures.append(f"item dimensions contain invalid measurements: {row_id}")
        if row.get("unit") not in {"mm", "cm", "m", "in", "ft"}:
            failures.append(f"item dimensions have an invalid unit: {row_id}")
        require_date("item dimensions", row, row_id, "measured_on")
        require_timestamp("item dimensions", row, row_id, "recorded_at")
        if (item_id, evidence_id) not in item_evidence:
            failures.append(f"item-dimension evidence does not support item: {row_id}")
        require_not_lower(
            "item dimensions",
            row,
            row_id,
            ("item", item_sensitivity.get(item_id)),
            ("evidence", evidence_sensitivity.get(evidence_id)),
        )
    amendments_by_item: dict[object, list[dict]] = {}
    for row in rows["item_amendments"]:
        row_id = row.get("amendment_id")
        item_id = row.get("item_id")
        evidence_id = row.get("evidence_id")
        require_date("item amendment", row, row_id, "amended_on")
        require_timestamp("item amendment", row, row_id, "recorded_at")
        if (item_id, evidence_id) not in item_evidence:
            failures.append(f"item-amendment evidence does not support item: {row_id}")
        item = items_by_id.get(item_id, {})
        identity_sensitivity = item.get("identity_sensitivity") or item.get("sensitivity")
        if (
            rank(identity_sensitivity) is not None
            and rank(evidence_sensitivity.get(evidence_id)) is not None
            and rank(identity_sensitivity) < rank(evidence_sensitivity.get(evidence_id))
        ):
            failures.append(
                f"item identity sensitivity is lower than correction evidence: {row_id}"
            )
        if row.get("previous_model_id") == row.get("target_model_id"):
            failures.append(f"item amendment does not change model identity: {row_id}")
        amendments_by_item.setdefault(item_id, []).append(row)
    current_model_by_item = {row.get("item_id"): row.get("model_id") for row in rows["items"]}
    for item_id, amendments in amendments_by_item.items():
        ordered_amendments = sorted(
            amendments,
            key=lambda row: (
                row.get("amended_on", ""),
                row.get("recorded_at", ""),
                row.get("amendment_id", ""),
            ),
        )
        for previous, current in zip(ordered_amendments, ordered_amendments[1:]):
            if previous.get("target_model_id") != current.get("previous_model_id"):
                failures.append(f"item-amendment identity chain is broken: {item_id}")
        if ordered_amendments[-1].get("target_model_id") != current_model_by_item.get(item_id):
            failures.append(f"item model disagrees with latest amendment: {item_id}")

    def decoded_canonical_object(value: object, label: str) -> dict | None:
        if not isinstance(value, str):
            failures.append(f"{label} is not JSON text")
            return None
        try:
            decoded = strict_json_loads(value, label=label)
        except StrictJSONError:
            failures.append(f"{label} is malformed")
            return None
        if not isinstance(decoded, dict) or strict_json_dumps(decoded, sort_keys=True) != value:
            failures.append(f"{label} is not a canonical JSON object")
            return None
        return decoded

    def valid_item_details(value: dict, label: str) -> bool:
        valid = True
        acquired_on = value.get("acquired_on")
        if acquired_on is not None:
            try:
                acquired_valid = (
                    isinstance(acquired_on, str)
                    and date.fromisoformat(acquired_on).isoformat() == acquired_on
                )
            except ValueError:
                acquired_valid = False
            if not acquired_valid:
                failures.append(f"{label} has invalid acquired_on")
                valid = False
        for field in ("condition", "receipt_ref", "serial_or_lot"):
            if value.get(field) is not None and not isinstance(value.get(field), str):
                failures.append(f"{label} has invalid {field}")
                valid = False
        amount = value.get("purchase_price")
        currency = value.get("purchase_currency")
        if (amount is None) != (currency is None) or (
            amount is not None
            and (
                type(amount) not in {int, float}
                or not math.isfinite(amount)
                or amount < 0
                or not isinstance(currency, str)
                or re.fullmatch(r"[A-Z]{3}", currency) is None
            )
        ):
            failures.append(f"{label} has invalid purchase value")
            valid = False
        return valid

    detail_chains: dict[object, list[tuple[dict, dict, dict]]] = {}
    for row in rows["item_detail_amendments"]:
        row_id = row.get("detail_amendment_id")
        item_id = row.get("item_id")
        evidence_id = row.get("evidence_id")
        require_date("item detail amendment", row, row_id, "amended_on")
        require_timestamp("item detail amendment", row, row_id, "recorded_at")
        if not isinstance(row.get("actor"), str) or not row["actor"].strip():
            failures.append(f"item detail amendment has blank actor: {row_id}")
        previous = decoded_canonical_object(
            row.get("previous_json"), f"item detail amendment {row_id} previous_json"
        )
        changes = decoded_canonical_object(
            row.get("changes_json"), f"item detail amendment {row_id} changes_json"
        )
        if previous is None or changes is None:
            continue
        if set(previous) != set(ITEM_DETAIL_FIELDS):
            failures.append(f"item detail amendment has incomplete predecessor: {row_id}")
            continue
        if not changes or not set(changes).issubset(ITEM_DETAIL_FIELDS):
            failures.append(f"item detail amendment has invalid changes: {row_id}")
            continue
        resulting = {**previous, **changes}
        valid_item_details(previous, f"item detail amendment {row_id} predecessor")
        valid_item_details(resulting, f"item detail amendment {row_id} result")
        reset_confirmation = (
            changes == {field: None for field in REACQUISITION_RESET_FIELDS}
            and any(
                event.get("event_type") == "reacquired"
                and event.get("item_id") == item_id
                and event.get("evidence_id") == evidence_id
                and event.get("occurred_on") == row.get("amended_on")
                for event in rows["inventory_events"]
            )
        )
        if (
            all(previous.get(field) == value for field, value in changes.items())
            and not reset_confirmation
        ):
            failures.append(f"item detail amendment makes no change: {row_id}")
        if (item_id, evidence_id) not in item_evidence:
            failures.append(f"item-detail amendment evidence does not support item: {row_id}")
        require_not_lower(
            "item detail amendment",
            row,
            row_id,
            ("item", item_sensitivity.get(item_id)),
            ("evidence", evidence_sensitivity.get(evidence_id)),
        )
        detail_chains.setdefault(item_id, []).append((row, previous, resulting))
    for item_id, chain in detail_chains.items():
        ordered = sorted(
            chain,
            key=lambda entry: (
                entry[0].get("amended_on", ""),
                entry[0].get("recorded_at", ""),
                entry[0].get("detail_amendment_id", ""),
            ),
        )
        for prior, current in zip(ordered, ordered[1:]):
            if prior[2] != current[1]:
                failures.append(f"item-detail amendment chain is broken: {item_id}")
        item = items_by_id.get(item_id, {})
        current_details = {field: item.get(field) for field in ITEM_DETAIL_FIELDS}
        if ordered[-1][2] != current_details:
            failures.append(f"item details disagree with latest amendment: {item_id}")

    ordered_detail_chains = {
        item_id: sorted(
            chain,
            key=lambda entry: (
                entry[0].get("amended_on", ""),
                entry[0].get("recorded_at", ""),
                entry[0].get("detail_amendment_id", ""),
            ),
        )
        for item_id, chain in detail_chains.items()
    }
    events_by_item: dict[object, list[dict]] = {}
    for event in rows["inventory_events"]:
        events_by_item.setdefault(event.get("item_id"), []).append(event)
    for event in rows["inventory_events"]:
        if event.get("event_type") != "reacquired":
            continue
        item_id = event.get("item_id")
        event_date = event.get("occurred_on")
        try:
            declaration = strict_json_loads(
                event.get("details_json"),
                label=f"reacquired event {event.get('event_id')} details_json",
            )
        except (StrictJSONError, TypeError):
            declaration = {}
        chain = ordered_detail_chains.get(item_id, [])
        reset_amendments = [
            (amendment, resulting)
            for amendment, _previous, resulting in chain
            if amendment.get("evidence_id") == event.get("evidence_id")
            and amendment.get("amended_on") == event_date
            and strict_json_loads(
                amendment.get("changes_json"),
                label=(
                    f"item detail amendment "
                    f"{amendment.get('detail_amendment_id')} changes_json"
                ),
            )
            == {field: None for field in REACQUISITION_RESET_FIELDS}
            and all(resulting.get(field) is None for field in REACQUISITION_RESET_FIELDS)
        ]
        if not reset_amendments:
            failures.append(
                f"reacquired item lacks an ownership-episode reset artifact: {item_id}"
            )

        declared_condition = (
            declaration.get("condition_checked")
            if isinstance(declaration, dict)
            else None
        )
        if declared_condition is not None and not any(
            amendment.get("evidence_id") == event.get("evidence_id")
            and amendment.get("amended_on") == event_date
            and resulting.get("condition") == declared_condition
            and previous.get("condition") != declared_condition
            for amendment, previous, resulting in chain
        ):
            failures.append(
                f"reacquired item condition lacks current reaffirmation: {item_id}"
            )

        quantity_before = items_by_id.get(item_id, {}).get("quantity")
        item_events = sorted(
            events_by_item.get(item_id, []),
            key=lambda row: row.get("sequence", -1),
            reverse=True,
        )
        for later in item_events:
            if later.get("sequence", -1) <= event.get("sequence", -1):
                continue
            if later.get("event_type") not in {"planned", "ordered", "quantity_changed"}:
                continue
            raw_details = later.get("details_json")
            if raw_details is None:
                continue
            try:
                quantity_details = strict_json_loads(
                    raw_details,
                    label=f"inventory event {later.get('event_id')} details_json",
                )
            except StrictJSONError:
                continue
            if isinstance(quantity_details, dict):
                quantity_before = quantity_details.get("previous_quantity")
        declared_quantity = (
            declaration.get("quantity_checked")
            if isinstance(declaration, dict)
            else None
        )
        declared_unit = (
            declaration.get("unit") if isinstance(declaration, dict) else None
        )
        if quantity_before is not None or declared_quantity is not None:
            reaffirmations = [
                later
                for later in events_by_item.get(item_id, [])
                if later.get("event_type") == "quantity_changed"
                and later.get("evidence_id") == event.get("evidence_id")
                and later.get("occurred_on") == event_date
                and later.get("sequence", -1) > event.get("sequence", -1)
            ]
            matching_reaffirmation = False
            for reaffirmation in reaffirmations:
                try:
                    quantity_details = strict_json_loads(
                        reaffirmation.get("details_json"),
                        label=(
                            f"inventory event {reaffirmation.get('event_id')} "
                            "details_json"
                        ),
                    )
                except (StrictJSONError, TypeError):
                    continue
                if (
                    isinstance(quantity_details, dict)
                    and quantity_details.get("quantity") == declared_quantity
                    and quantity_details.get("unit") == declared_unit
                ):
                    matching_reaffirmation = True
                    break
            if not matching_reaffirmation:
                failures.append(
                    f"reacquired item quantity lacks current confirmation: {item_id}"
                )

    def fact_item_ids(table: str, row: dict) -> set[object]:
        if table in {
            "aliases",
            "item_tags",
            "valuations",
            "item_documents",
            "item_dimensions",
        }:
            return {row.get("item_id")}
        if table == "relationships":
            return {row.get("subject_item_id"), row.get("object_item_id")}
        if table == "torque_paths":
            return {row.get("tool_item_id")}
        if table == "kits":
            return {row.get("serves_item_id")}
        if table == "kit_requirements":
            return {row.get("item_id")}
        if table == "model_interfaces":
            return set(items_by_model.get(row.get("model_id"), set()))
        return set()

    fact_chains: dict[tuple[str, str], list[tuple[dict, dict, dict | None]]] = {}
    for row in rows["fact_amendments"]:
        row_id = row.get("fact_amendment_id")
        table = row.get("table_name")
        require_date("fact amendment", row, row_id, "amended_on")
        require_timestamp("fact amendment", row, row_id, "recorded_at")
        if not isinstance(row.get("actor"), str) or not row["actor"].strip():
            failures.append(f"fact amendment has blank actor: {row_id}")
        if not isinstance(row.get("reason"), str) or not row["reason"].strip():
            failures.append(f"fact amendment has blank reason: {row_id}")
        if table not in FACT_SELECTOR_FIELDS:
            failures.append(f"fact amendment names unsupported table: {row_id}")
            continue
        selector = decoded_canonical_object(
            row.get("selector_json"), f"fact amendment {row_id} selector_json"
        )
        previous = decoded_canonical_object(
            row.get("previous_json"), f"fact amendment {row_id} previous_json"
        )
        replacement = None
        if row.get("replacement_json") is not None:
            replacement = decoded_canonical_object(
                row.get("replacement_json"),
                f"fact amendment {row_id} replacement_json",
            )
        if selector is None or previous is None:
            continue
        selector_fields = FACT_SELECTOR_FIELDS[table]
        if set(selector) != set(selector_fields) or any(
            not isinstance(selector.get(field), str) or not selector[field]
            for field in selector_fields
        ):
            failures.append(f"fact amendment has invalid selector: {row_id}")
            continue
        if set(previous) != FACT_ROW_FIELDS[table] or (
            replacement is not None and set(replacement) != FACT_ROW_FIELDS[table]
        ):
            failures.append(f"fact amendment has invalid row shape: {row_id}")
            continue
        if any(previous.get(field) != selector[field] for field in selector_fields) or (
            replacement is not None
            and any(replacement.get(field) != selector[field] for field in selector_fields)
        ):
            failures.append(f"fact amendment changes its identity: {row_id}")
        action = row.get("action")
        if (action == "replace") != (replacement is not None):
            failures.append(f"fact amendment action disagrees with replacement: {row_id}")
        if replacement == previous:
            failures.append(f"fact amendment makes no change: {row_id}")
        evidence_id = row.get("evidence_id")
        affected = {
            item_id
            for item_id in fact_item_ids(table, previous)
            | (fact_item_ids(table, replacement) if replacement is not None else set())
            if item_id is not None
        }
        evidence_items_for_amendment = {
            item_id
            for item_id in fact_item_ids(
                table, replacement if replacement is not None else previous
            )
            if item_id is not None
        }
        for item_id in evidence_items_for_amendment:
            if (item_id, evidence_id) not in item_evidence:
                failures.append(f"fact-amendment evidence does not support affected item: {row_id}")
        if (
            replacement is not None
            and "evidence_id" in replacement
            and (replacement.get("evidence_id") != evidence_id)
        ):
            failures.append(f"fact amendment replacement uses different evidence: {row_id}")
        parents: list[tuple[str, object]] = [("evidence", evidence_sensitivity.get(evidence_id))]
        parents.extend(("item", item_sensitivity.get(item_id)) for item_id in affected)
        for fact in (previous, replacement):
            if fact is not None and fact.get("sensitivity") in SENSITIVITY_RANK:
                parents.append(("fact", fact["sensitivity"]))
        if table == "locations":
            parents.append(("location", previous.get("sensitivity")))
            parent_id = previous.get("parent_location_id")
            parents.append(("location ancestry", location_ancestry_sensitivity(parent_id)))
        elif table == "spatial_profiles":
            parents.append(
                (
                    "location ancestry",
                    location_ancestry_sensitivity(previous.get("location_id")),
                )
            )
        require_not_lower("fact amendment", row, row_id, *parents)
        key = (table, row["selector_json"])
        fact_chains.setdefault(key, []).append((row, previous, replacement))
    for (table, selector_json), chain in fact_chains.items():
        ordered = sorted(
            chain,
            key=lambda entry: (
                entry[0].get("amended_on", ""),
                entry[0].get("recorded_at", ""),
                entry[0].get("fact_amendment_id", ""),
            ),
        )
        for prior, current in zip(ordered, ordered[1:]):
            if prior[2] is None or prior[2] != current[1]:
                failures.append(f"fact amendment chain is broken: {table}/{selector_json}")
        selector = strict_json_loads(selector_json, label="fact selector")
        current_rows = [
            fact
            for fact in rows[table]
            if all(fact.get(field) == value for field, value in selector.items())
        ]
        final = ordered[-1][2]
        if (final is None and current_rows) or (
            final is not None and (len(current_rows) != 1 or current_rows[0] != final)
        ):
            failures.append(f"current fact disagrees with amendment chain: {table}/{selector_json}")
    for row in rows["capture_sessions"]:
        row_id = row.get("capture_session_id")
        require_date("capture session", row, row_id, "captured_on")
        require_not_lower(
            "capture session",
            row,
            row_id,
            ("evidence", evidence_sensitivity.get(row.get("evidence_id"))),
        )
    for row in rows["capture_observations"]:
        row_id, item_id, evidence_id = (
            row.get("observation_id"),
            row.get("item_id"),
            row.get("evidence_id"),
        )
        require_json_type("capture observation", row, row_id, "observation_json", dict)
        if item_id is not None and (item_id, evidence_id) not in item_evidence:
            failures.append(f"capture observation evidence does not support item: {row_id}")
        require_not_lower(
            "capture observation",
            row,
            row_id,
            ("item", item_sensitivity.get(item_id)),
            ("evidence", evidence_sensitivity.get(evidence_id)),
            ("capture session", session_sensitivity.get(row.get("capture_session_id"))),
        )
    for row in rows["maintenance_sessions"]:
        row_id, evidence_id = row.get("maintenance_session_id"), row.get("evidence_id")
        require_date("maintenance session", row, row_id, "performed_on")
        for field in ("elapsed_seconds", "correction_count", "review_count"):
            value = row.get(field)
            if type(value) is not int or value < 0:
                failures.append(f"maintenance session has invalid {field}: {row_id}")
        parents = [("evidence", evidence_sensitivity.get(evidence_id))]
        for item_id in maintenance_items.get(row_id, set()):
            if (item_id, evidence_id) not in item_evidence:
                failures.append(f"maintenance evidence does not support item: {row_id}")
            parents.append(("item", item_sensitivity.get(item_id)))
        require_not_lower("maintenance session", row, row_id, *parents)
    for row in rows["sync_receipts"]:
        row_id = row.get("sync_receipt_id")
        require_timestamp("sync receipt", row, row_id, "recorded_at")
        require_not_lower(
            "sync receipt",
            row,
            row_id,
            ("evidence", evidence_sensitivity.get(row.get("evidence_id"))),
        )
    failures.extend(capture_provenance_failures(rows))
    return failures


def read_jsonl(path: Path) -> list[dict]:
    try:
        rows: list[dict] = []
        for number, line in enumerate(path.read_text().splitlines(), start=1):
            if not line.strip():
                continue
            row = strict_json_loads(line, label=f"{path.name} line {number}")
            if not isinstance(row, dict):
                raise StrictJSONError(f"{path.name} line {number} is not an object")
            rows.append(row)
        return rows
    except (OSError, UnicodeError, StrictJSONError) as error:
        raise SchemaVersionError(f"cannot read {path}: {error}") from error


def canonical_store_digest(store: Path) -> str:
    """Hash the exact canonical table filenames and bytes in stable order."""
    digest = hashlib.sha256()
    for path in sorted(store.glob("*.jsonl")):
        digest.update(path.name.encode("utf-8"))
        digest.update(b"\0")
        try:
            with path.open("rb") as handle:
                for block in iter(lambda: handle.read(1024 * 1024), b""):
                    digest.update(block)
        except OSError as error:
            raise SchemaVersionError(f"cannot hash {path}: {error}") from error
        digest.update(b"\0")
    return digest.hexdigest()


def lexical_absolute(path: Path) -> Path:
    """Make a path absolute without resolving away symlink evidence."""
    return Path(os.path.abspath(path))


def validate_store_paths(store: Path) -> None:
    """Reject path aliases before any canonical byte is read."""
    if store.parent.is_symlink():
        raise SchemaVersionError("canonical Data directory must not be a symlink")
    if store.is_symlink() or not store.is_dir():
        raise SchemaVersionError("canonical store must be a real directory, not a symlink")
    expected_entries = {f"{table}.jsonl" for table in TABLES}
    actual_entries = {entry.name for entry in store.iterdir()}
    if actual_entries != expected_entries:
        extras = sorted(actual_entries - expected_entries)
        missing = sorted(expected_entries - actual_entries)
        detail = []
        if extras:
            detail.append("unexpected: " + ", ".join(extras))
        if missing:
            detail.append("missing: " + ", ".join(missing))
        raise SchemaVersionError(
            "canonical store must contain exactly schema tables (" + "; ".join(detail) + ")"
        )
    for table in TABLES:
        path = store / f"{table}.jsonl"
        if path.is_symlink():
            raise SchemaVersionError(f"canonical table must not be a symlink: {path.name}")
        if not path.is_file():
            raise SchemaVersionError(f"missing canonical table: {path.name}")


def read_schema_metadata(store: Path) -> dict:
    """Read the one authoritative schema record without touching SQLite."""
    metadata_path = store / "metadata.jsonl"
    if not metadata_path.exists():
        raise SchemaVersionError("missing canonical table: metadata.jsonl")
    metadata = read_jsonl(metadata_path)
    if len(metadata) != 1:
        raise SchemaVersionError("metadata.jsonl must contain exactly one record")
    record = metadata[0]
    if set(record) != {"inventory_id", "schema_version"}:
        raise SchemaVersionError(
            "metadata.jsonl record must contain exactly inventory_id and schema_version"
        )
    if not isinstance(record["inventory_id"], str) or not record["inventory_id"].strip():
        raise SchemaVersionError("metadata inventory_id must be a non-empty string")
    version = record["schema_version"]
    if type(version) is not int:
        raise SchemaVersionError("metadata schema_version must be an integer")
    if version > SCHEMA_VERSION:
        raise SchemaVersionError(
            f"inventory schema {version} is newer than supported schema {SCHEMA_VERSION}"
        )
    if version != SCHEMA_VERSION:
        raise SchemaVersionError(
            f"inventory schema {version} requires migration to schema {SCHEMA_VERSION}"
        )
    return record


def load_store_current(store: Path) -> dict[str, list[dict]]:
    """Load a complete current store before replacing the SQLite projection."""
    validate_store_paths(store)
    read_schema_metadata(store)
    rows: dict[str, list[dict]] = {}
    for table in TABLES:
        path = store / f"{table}.jsonl"
        rows[table] = read_jsonl(path)
    return rows


def insert_rows(con: sqlite3.Connection, table: str, rows: list[dict]) -> None:
    if not rows:
        return
    columns = tuple(row[1] for row in con.execute(f'PRAGMA table_info("{table}")'))
    column_set = set(columns)
    for number, row in enumerate(rows, start=1):
        if set(row) != column_set:
            raise SchemaVersionError(f"{table}.jsonl row {number} does not match schema columns")
    quoted_columns = ",".join(f'"{column}"' for column in columns)
    placeholders = ",".join("?" for _ in columns)
    con.executemany(
        f'INSERT INTO "{table}" ({quoted_columns}) VALUES ({placeholders})',
        [[row[column] for column in columns] for row in rows],
    )


def rebuild(store: Path, schema: Path, database: Path) -> dict:
    """Atomically replace ``database`` with a projection of a valid current store."""
    rows = load_store_current(store)
    if failures := semantic_failures(rows):
        raise SchemaVersionError("; ".join(failures))
    if not schema.exists():
        raise SchemaVersionError(f"schema file is missing: {schema}")
    if database.is_symlink() or database.parent.is_symlink():
        raise SchemaVersionError("database path must not contain a managed symlink")

    database.parent.mkdir(parents=True, exist_ok=True)
    temporary = database.with_name(f".{database.name}.rebuild-{os.getpid()}")
    temporary.unlink(missing_ok=True)
    con: sqlite3.Connection | None = None
    try:
        con = sqlite3.connect(temporary)
        os.chmod(temporary, 0o600, follow_symlinks=False)
        con.executescript(schema.read_text())
        for table in TABLES:
            insert_rows(con, table, rows[table])
        con.execute(
            "INSERT INTO projection_state(store_digest) VALUES (?)",
            (canonical_store_digest(store),),
        )
        con.execute(
            "INSERT INTO inventory_search(item_id,name,brand,model,category,interfaces,specs,notes) "
            "SELECT item_id,name,brand,model,category,interfaces_json,specs_json,notes FROM v_inventory"
        )
        con.commit()
        failures = con.execute("PRAGMA foreign_key_check").fetchall()
        if failures:
            raise SchemaVersionError(f"foreign-key failures: {failures}")
        counts = {
            table: con.execute(f'SELECT count(*) FROM "{table}"').fetchone()[0] for table in TABLES
        }
    except BaseException:
        if con is not None:
            con.close()
        temporary.unlink(missing_ok=True)
        raise
    else:
        con.close()
        os.replace(temporary, database)
        os.chmod(database, 0o600, follow_symlinks=False)
    return {"database": str(database), "counts": counts, "foreign_key_failures": 0}


def main() -> None:
    here = Path(__file__).resolve().parent
    parser = argparse.ArgumentParser()
    parser.add_argument("--store", type=Path, required=True)
    parser.add_argument("--schema", type=Path, default=here / "schema.sql")
    parser.add_argument("--database", type=Path, default=here / ".local" / "inventory.sqlite")
    args = parser.parse_args()

    try:
        result = rebuild(
            lexical_absolute(args.store),
            lexical_absolute(args.schema),
            lexical_absolute(args.database),
        )
    except SchemaVersionError as error:
        raise SystemExit(str(error)) from error
    print(json.dumps(result, indent=2))


if __name__ == "__main__":
    main()
